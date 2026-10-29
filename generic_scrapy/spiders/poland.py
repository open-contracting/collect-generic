__review_status__ = "unreviewed"
__generated_by__ = "claude-code/claude-opus-5-5"
__generated_at__ = "2026-10-29"

import collections
import csv
import datetime as dt
import json
import re
from http import HTTPStatus
from pathlib import Path
from urllib.parse import quote, urlencode, urlparse

import scrapy
from scrapy.http import JsonRequest

from generic_scrapy.base_spiders.base_spider import BaseSpider

UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")

# Matches an h3 prefix in a BZP announcement like "1.5.1.) Ulica" or
# "1.4) Krajowy Numer Identyfikacyjny: REGON 180792174".
# Group 1: dotted key (e.g. "1.5.1"). Group 2: label. Group 3 (optional): post-colon remainder.
ANNOUNCEMENT_KEY_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)*)\.?\)\s*(.*?)(?::\s*(.*))?$", re.DOTALL)

TENDER_ID_RE = re.compile(r"ocds-148610-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
PLATFORM_URL_RE = re.compile(r"Adres strony internetowej prowadzonego postępowania:?\s*(\S+)")
TED_OJS_RE = re.compile(r"(\d{4})/S \d{3}-(\d{6})$")
TED_EFORMS_RE = re.compile(r"\d+-\d{4}$")
URL_DOCUMENT_RE = re.compile(r"<URL_DOCUMENT>\s*([^<\s]+)")

# Documents in this state have no file ("Brak załącznika dla wskazanego dokumentu").
DELETED_AUTOMATICALLY = "DeletedAutomatically"
BOARD_NOTICE_TYPES = ("ContractNotice", "NoticeUpdateNotice")
TED_URL_FIELDS = ("document-url-lot", "document-restricted-url-lot", "submission-url-lot")
TED_FIELDS = [
    "publication-number",
    "publication-date",
    "notice-type",
    "procedure-identifier",
    "deadline-receipt-tender-date-lot",
    *TED_URL_FIELDS,
]
# Board/Search ignores PageSize above 10, so cap the pages read when listing an organization's notices.
MAX_NOTICE_PAGES = 30

# Checked in order against the document's name and filename.
ROLES = [
    ("notice_change", re.compile(r"zmian\w* ogłosz|ogłoszeni\w* o zmianie|sprostowani")),
    ("swz_change", re.compile(r"zmian\w* (?:treści )?(?:swz|specyfikacji)|modyfikacj")),
    ("qa", re.compile(r"wyjaśnie|odpowied\w* na pytani|pytania i odpowiedzi|\bpytani|\bpyt\w* i odp")),
    ("opz", re.compile(r"\bopz\b|opis\w* przedmiotu zamówienia|\bpfu\b|program\w* funkcjonalno")),
    ("ppu", re.compile(r"\bppu\b|projektowan\w* postanowie|wz[oó]r umowy|istotne postanowienia")),
    ("notice", re.compile(r"^ogłoszeni|\bojs\d{3}\b")),
    ("annex", re.compile(r"załącznik|\bza[lł]\.?\s*(?:nr|\d)|\bzal\b")),
    ("swz", re.compile(r"\bswz\b|specyfikacj\w* warunków zamówienia")),
]
PRIORITY_GROUPS = [
    ("cn_test", lambda row: row.get("cn_split") == "test"),
    ("art124_test", lambda row: row.get("art124_split") == "test"),
    ("dev", lambda row: "dev" in {row.get("cn_split"), row.get("art124_split")}),
    ("art124_train", lambda row: row.get("art124_split") == "train"),
    ("cn_aux", lambda row: row.get("cn_split") == "aux"),
]
GROUP_ORDER = {name: i for i, (name, _) in enumerate(PRIORITY_GROUPS)}


def _norm(text):
    return " ".join((text or "").split())


def _text(selector):
    return _norm(" ".join(selector.xpath(".//text()").getall()))


def _parse_announcement_html(html):
    """
    Parse a Polish BZP announcement (mo-board GetNoticeHtmlBody) into a structured dict.

    Returns ``{"title": str|None, "sections": [...], "_unparsed": [...]}`` where each section
    is ``{"title": str, "items": [{"key", "label", "value", "extra"}]}``.
    """
    sel = scrapy.Selector(text=html)

    title = _text(sel.xpath("//h1[1]")) or None
    sections = []
    current = None
    unparsed = []

    for node in sel.xpath("//h2 | //h3 | //p"):
        tag = node.root.tag
        if tag == "h2":
            if current is not None:
                sections.append(current)
            current = {"title": _text(node), "items": []}
        elif tag == "h3":
            full_text = _text(node)
            span_text = _text(node.xpath("span[contains(@class, 'normal')]")) or None
            match = ANNOUNCEMENT_KEY_RE.match(full_text)
            if not match:
                unparsed.append({"section": current["title"] if current else None, "text": full_text})
                continue
            key, label, after_colon = match.group(1), _norm(match.group(2)), _norm(match.group(3) or "") or None
            value = span_text or after_colon
            item = {"key": key, "label": label, "value": value, "extra": None}
            (current["items"] if current is not None else unparsed).append(item)
        elif tag == "p":
            text = _text(node)
            if not text or not current or not current["items"]:
                continue
            last = current["items"][-1]
            last["extra"] = f"{last['extra']} {text}" if last["extra"] else text

    if current is not None:
        sections.append(current)

    return {"title": title, "sections": sections, "_unparsed": unparsed}


def role(*names):
    """Return the document's role (swz, opz, ppu, annex, swz_change, qa, notice, notice_change or other)."""
    text = " ".join(_norm(name).lower().replace("_", " ") for name in names if name)
    return next((name for name, pattern in ROLES if pattern.search(text)), "other")


def ted_publication_number(notice):
    """Return the TED API publication number (``NNNNNN-YYYY``) for an OJ S or eForms notice number."""
    if match := TED_OJS_RE.match(notice):
        return f"{int(match.group(2))}-{match.group(1)}"
    if TED_EFORMS_RE.match(notice):
        return notice
    return None


def host(url):
    return urlparse(url if "://" in url else f"https://{url}").netloc.lower().split(":")[0].removeprefix("www.")


def parse_datetime(value):
    """Parse an ISO 8601 datetime, or a TED date like ``2026-03-09+01:00``."""
    if not value:
        return None
    value = value.replace("Z", "+00:00")
    # Python 3.10 accepts only 3- or 6-digit fractions.
    value = re.sub(r"\.(\d+)", lambda m: f".{m.group(1)[:6]:0<6}", value)
    if re.match(r"\d{4}-\d{2}-\d{2}[+-]", value):
        value = f"{value[:10]}T23:59:59{value[10:]}"
    return dt.datetime.fromisoformat(value)


class Poland(BaseSpider):
    """
    Per-procedure file collector for the Polish public-procurement portal (ezamowienia.gov.pl).

    Entry modes:

    - Default: walks the SearchTenders catalogue (``Initiated`` tenders only).
    - ``-a from_date=YYYY-MM-DD -a until_date=YYYY-MM-DD``: walks contract notices in Board/Search by publication
      date, both days included, which reaches closed tenders too.
    - ``-a notice_numbers=<csv>``: resolves each control's BZP or TED notices to its procedure, for the oecd-poland
      control analysis. ``-a priority=cn_test,art124_test`` selects priority groups.

    For each tender, writes:
      - tender.json         — Search/GetTender response
      - documents.json      — Search/GetTenderDocuments response (every document version, including deleted ones)
      - notices.json        — the tender's contract notices and notice changes from Board/Search
      - notices/<number>.{html,json} — each notice's Board/GetNoticeHtmlBody, raw and parsed
      - attachments/<documentId>__<filename> — each document that still has a file

    The first two modes write to ``<FILES_STORE>/poland/<crawl_directory>/<tenderId>/``. Re-running with the same
    ``crawl_time`` skips tenders whose ``tender.json`` is already on disk. The ``notice_numbers`` mode writes to
    ``<FILES_STORE>/poland_tender_documents/<crawl_directory>/<control_id>/<tenderId>/``, plus a ``manifest.json``
    (one JSON line per control) and a ``coverage.json`` report.
    """

    name = "poland"

    # BaseSpider
    default_from_date = "2021-01-01"

    SEARCH_URL = (
        "https://ezamowienia.gov.pl/mp-readmodels/api/Search/SearchTenders"
        "?Page={page}&PageSize=100&SortingColumnName=InitiationDate&SortingDirection=DESC"
    )
    BOARD_SEARCH_URL = "https://ezamowienia.gov.pl/mo-board/api/v1/Board/Search?{query}"
    TENDER_URL = "https://ezamowienia.gov.pl/mp-readmodels/api/Search/GetTender?id={id}"
    DOCUMENTS_URL = "https://ezamowienia.gov.pl/mp-readmodels/api/Search/GetTenderDocuments?tenderId={id}"
    DOWNLOAD_URL = "https://ezamowienia.gov.pl/mp-readmodels/api/Tender/DownloadDocument/{tender_id}/{doc_id}"
    NOTICE_HTML_URL = "https://ezamowienia.gov.pl/mo-board/api/v1/Board/GetNoticeHtmlBody?noticeNumber={n}"
    PROCEDURE_URL = "https://ezamowienia.gov.pl/mp-client/search/list/{id}"
    TED_SEARCH_URL = "https://api.ted.europa.eu/v3/notices/search"
    TED_XML_URL = "https://ted.europa.eu/en/notice/{n}/xml"
    TED_NOTICE_URL = "https://ted.europa.eu/en/notice/-/detail/{n}"

    custom_settings = {
        "CONCURRENT_REQUESTS_PER_DOMAIN": 2,
        "DOWNLOAD_DELAY": 0.5,
        # The TED Search API answers bursts with HTTP 429, and ted.europa.eu with a WAF challenge.
        "DOWNLOAD_SLOTS": {
            "api.ted.europa.eu": {"concurrency": 1, "delay": 2},
            "ted.europa.eu": {"concurrency": 1, "delay": 3},
        },
        "RETRY_TIMES": 5,
        # Board/Search can take a minute to answer.
        "DOWNLOAD_TIMEOUT": 180,
        # Only attachments and notice bodies are cached (API calls set dont_cache), so re-runs see new documents.
        "HTTPCACHE_ENABLED": True,
        "HTTPCACHE_DIR": "data/poland/_httpcache",
        "HTTPCACHE_EXPIRATION_SECS": 0,
        "HTTPCACHE_IGNORE_HTTP_CODES": [202, 400, 403, 404, 429, 500, 502, 503, 504],
        "DOWNLOAD_MAXSIZE": 512 * 1024 * 1024,
        "DOWNLOAD_WARNSIZE": 128 * 1024 * 1024,
    }

    def __init__(self, *args, notice_numbers=None, priority=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.notice_numbers = notice_numbers
        self.priority = priority.split(",") if priority else None
        self.manifest = []
        self.skipped = []
        self._yielded = 0
        self._seen = set()

    async def start(self):
        if self.notice_numbers:
            for request in self.control_requests():
                yield request
        elif self.from_date:
            day = self.from_date
            while day <= self.until_date:
                yield self.board_search_request(
                    {
                        "NoticeType": "ContractNotice",
                        "PublicationDateFrom": day.strftime("%Y-%m-%dT00:00:00"),
                        "PublicationDateTo": day.strftime("%Y-%m-%dT23:59:59"),
                    },
                    callback=self.parse_board_search,
                )
                day += dt.timedelta(days=1)
        else:
            yield scrapy.Request(self.SEARCH_URL.format(page=1), meta={"dont_cache": True}, callback=self.parse_search)

    # Default and date modes.

    def parse_search(self, response):
        pagination = json.loads(response.headers.get("X-Pagination", b"{}").decode() or "{}")
        for summary in response.json():
            if request := self.tender_request(summary["objectId"]):
                yield request
            if self.sample and self._yielded >= self.sample:
                return
        if pagination.get("HasNext"):
            yield scrapy.Request(
                self.SEARCH_URL.format(page=pagination["CurrentPage"] + 1),
                meta={"dont_cache": True},
                callback=self.parse_search,
            )

    def parse_board_search(self, response, query):
        pagination = json.loads(response.headers.get("X-Pagination", b"{}").decode() or "{}")
        data = response.json()
        if isinstance(data, dict):
            self.logger.warning("Board/Search error for %r: %s", query, str(data.get("error"))[:200])
            return
        for notice in data:
            if notice.get("tenderId") and (request := self.tender_request(notice["tenderId"])):
                yield request
            if self.sample and self._yielded >= self.sample:
                return
        if pagination.get("HasNext"):
            yield self.board_search_request(
                {**query, "PageNumber": pagination["CurrentPage"] + 1}, callback=self.parse_board_search
            )

    def tender_request(self, tender_id):
        if tender_id in self._seen or (self.tender_directory(tender_id) / "tender.json").exists():
            return None
        self._seen.add(tender_id)
        self._yielded += 1
        return scrapy.Request(
            self.TENDER_URL.format(id=tender_id),
            meta={"dont_cache": True},
            callback=self.parse_tender,
            cb_kwargs={"tender_id": tender_id},
        )

    async def parse_tender(self, response, tender_id):
        await self.collect_tender(tender_id, self.tender_directory(tender_id), response.json())

    def tender_directory(self, tender_id):
        return Path(self.settings["FILES_STORE"], self.name, self.crawl_directory, tender_id)

    # notice_numbers mode.

    def control_requests(self):
        with Path(self.notice_numbers).open(encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        controls = []
        for row in rows:
            group = next((name for name, test in PRIORITY_GROUPS if test(row)), None)
            if self.priority and group not in self.priority:
                continue
            notices = [notice for notice in row["notice_ids"].split("; ") if notice]
            control = {"control_id": row["control_id"], "group": group, "kind": row.get("kind"), "notices": notices}
            if not notices or row.get("missing_identifier") == "yes":
                self.skipped.append(control)
            else:
                controls.append(control)
        controls.sort(key=lambda control: GROUP_ORDER.get(control["group"], len(GROUP_ORDER)))
        if self.sample:
            controls = controls[: self.sample]
        for i, control in enumerate(controls):
            # A placeholder request starts each control; its callback makes the other requests itself.
            yield scrapy.Request(
                f"data:,{control['control_id']}",
                callback=self.parse_control,
                cb_kwargs={"control": control},
                priority=-i,
                dont_filter=True,
                meta={"dont_cache": True},
            )

    async def parse_control(self, _response, control):
        directory = Path(
            self.settings["FILES_STORE"], "poland_tender_documents", self.crawl_directory, control["control_id"]
        )
        row = {
            "control_id": control["control_id"],
            "group": control["group"],
            "kind": control["kind"],
            "notice_ids": control["notices"],
            "source": "unresolved",
            "procedure_url": None,
            "tender_ids": [],
            "bid_deadline_original": None,
            "bid_deadline_final": None,
            "documents": [],
            "status": "not_found",
            "reason": None,
        }
        resolutions = [await self.resolve(notice) for notice in control["notices"]]
        tender_ids = list(dict.fromkeys(r["tender_id"] for r in resolutions if r.get("tender_id")))
        ted_notices = {n["publication-number"]: n for r in resolutions for n in r.get("ted_notices", [])}
        deadlines = sorted(filter(None, (r.get("deadline") for r in resolutions)))
        row["bid_deadline_original"] = deadlines[0].isoformat() if deadlines else None

        for tender_id in tender_ids:
            tender, documents = await self.collect_tender(tender_id, directory / tender_id)
            for document in documents:
                if document.get("path"):
                    document["path"] = str(Path(control["control_id"], tender_id, document["path"]))
            row["documents"].extend(documents)
            row["tender_ids"].append(tender_id)
            if deadline := self.final_deadline(tender):
                row["bid_deadline_final"] = max(filter(None, [row["bid_deadline_final"], deadline.isoformat()]))
            row["bid_deadline_original"] = row["bid_deadline_original"] or self.first_deadline(tender)
        row["documents"].extend(self.ted_documents(ted_notices.values()))
        if not row["bid_deadline_final"] and len(deadlines) > 1:
            row["bid_deadline_final"] = deadlines[-1].isoformat()
        row["bid_deadline_final"] = row["bid_deadline_final"] or row["bid_deadline_original"]

        if final := parse_datetime(row["bid_deadline_final"]):
            for document in row["documents"]:
                if published := parse_datetime(document.get("published_at")):
                    document["after_deadline"] = published > final

        if tender_ids:
            row["source"] = "ezamowienia"
            row["procedure_url"] = self.PROCEDURE_URL.format(id=tender_ids[0])
            files = [d for d in row["documents"] if d.get("file_name")]
            saved = [d for d in files if d.get("path")]
            deleted = sum(d.get("state") == DELETED_AUTOMATICALLY for d in files)
            if not saved:
                row["reason"] = f"e-Zamówienia deleted the files ({deleted} of {len(files)} deleted automatically)"
            elif len(saved) < len(files):
                row["status"] = "partial"
                row["reason"] = f"{len(files) - len(saved)} of {len(files)} files unavailable"
            else:
                row["status"] = "complete"
        elif url := next((u for r in resolutions for u in r.get("urls", [])), None):
            row["source"] = f"platform:{host(url)}"
            row["procedure_url"] = url
            row["reason"] = "documents are on another platform"
        else:
            row["reason"] = "; ".join(r["error"] for r in resolutions if r.get("error")) or "no documents URL"

        self.manifest.append(row)

    async def resolve(self, notice):
        """Resolve a notice number to a tender ID or documents URL, with its notices and bid deadline."""
        if publication_number := ted_publication_number(notice):
            return await self.resolve_ted(publication_number)
        if "/BZP " in notice:
            return await self.resolve_bzp(notice)
        return {"error": f"{notice}: unsupported notice number"}

    async def resolve_bzp(self, notice):
        hits = await self.board_search({"NoticeNumber": notice})
        if not hits:
            return {"error": f"{notice}: not found in Board/Search"}
        hit = hits[0]
        result = {"deadline": parse_datetime(hit.get("submittingOffersDate"))}
        if hit.get("tenderId"):
            tender = await self.get_json(self.TENDER_URL.format(id=hit["tenderId"]))
            if isinstance(tender, dict) and tender.get("isProceedingOnPlatform"):
                return {**result, "tender_id": hit["tenderId"]}
        response = await self.fetch(self.NOTICE_HTML_URL.format(n=quote(hit["noticeNumber"], safe="")))
        if response.status == HTTPStatus.OK and (
            match := PLATFORM_URL_RE.search(_norm(" ".join(response.xpath("//text()").getall())))
        ):
            return {**result, "urls": [match.group(1)]}
        return {**result, "error": f"{notice}: no documents URL in the notice"}

    async def resolve_ted(self, publication_number):
        notices = await self.ted_search(f"publication-number={publication_number}")
        if isinstance(notices, int):
            return {"error": f"{publication_number}: TED search failed (HTTP {notices})"}
        if not notices:
            return {"error": f"{publication_number}: not found in TED"}
        notice = notices[0]
        if procedure := notice.get("procedure-identifier"):
            procedure_notices = await self.ted_search(f'procedure-identifier="{procedure}"')
            if isinstance(procedure_notices, list) and procedure_notices:
                notices = procedure_notices
        result = {"ted_notices": notices, "deadline": self.ted_deadline(notice)}

        urls = next(([u for u in notice.get(field) or [] if u] for field in TED_URL_FIELDS if notice.get(field)), [])
        if not urls:
            response = await self.fetch(self.TED_XML_URL.format(n=publication_number))
            if response.status == HTTPStatus.OK:
                urls = URL_DOCUMENT_RE.findall(response.text)
            elif response.status == HTTPStatus.ACCEPTED:
                return {**result, "error": f"{publication_number}: TED notice XML blocked (HTTP 202)"}
        for url in urls:
            if match := TENDER_ID_RE.search(url):
                return {**result, "tender_id": match.group(0)}
        return (
            {**result, "urls": list(dict.fromkeys(urls))}
            if urls
            else {**result, "error": f"{publication_number}: no documents URL"}
        )

    @staticmethod
    def ted_deadline(notice):
        return max(
            filter(None, map(parse_datetime, notice.get("deadline-receipt-tender-date-lot") or [])), default=None
        )

    def ted_documents(self, notices):
        documents = []
        contract_notices = 0
        for notice in sorted(notices, key=lambda n: (n.get("publication-date") or "", n["publication-number"])):
            if (notice.get("notice-type") or "").startswith("cn"):
                notice_role = "notice_change" if contract_notices else "notice"
                contract_notices += 1
            else:
                notice_role = "other"
            documents.append(
                {
                    "path": None,
                    "url": self.TED_NOTICE_URL.format(n=notice["publication-number"]),
                    "title": f"TED {notice['publication-number']} ({notice.get('notice-type')})",
                    "published_at": notice.get("publication-date"),
                    "role": notice_role,
                    "superseded": False,
                }
            )
        return documents

    @staticmethod
    def submission_terms(tender):
        return [term for term in tender.get("terms") or [] if term.get("termType") == "SubmissionOffersDate"]

    def final_deadline(self, tender):
        terms = [parse_datetime(t["term"]) for t in self.submission_terms(tender) if t.get("isValid")]
        return max(filter(None, terms), default=None)

    def first_deadline(self, tender):
        terms = self.submission_terms(tender)
        return terms[0]["term"] if terms else None

    def closed(self, _reason):
        if not self.notice_numbers:
            return
        directory = Path(self.settings["FILES_STORE"], "poland_tender_documents", self.crawl_directory)
        directory.mkdir(parents=True, exist_ok=True)
        self.manifest.sort(key=lambda row: (GROUP_ORDER.get(row["group"], len(GROUP_ORDER)), row["control_id"]))
        with (directory / "manifest.json").open("w", encoding="utf-8") as f:
            f.writelines(json.dumps(row, ensure_ascii=False) + "\n" for row in self.manifest)

        counts = collections.defaultdict(collections.Counter)
        for row in self.manifest:
            counts[row["group"]][row["status"]] += 1
        for control in self.skipped:
            counts[control["group"]]["skipped"] += 1
        coverage = {
            "by_group": {group: dict(counter) for group, counter in counts.items()},
            "by_source": dict(collections.Counter(row["source"] for row in self.manifest).most_common()),
            "not_found": [
                {key: row[key] for key in ("control_id", "group", "notice_ids", "source", "procedure_url", "reason")}
                for row in self.manifest
                if row["status"] != "complete"
            ],
            "skipped": [{key: c[key] for key in ("control_id", "group")} for c in self.skipped],
        }
        (directory / "coverage.json").write_text(json.dumps(coverage, ensure_ascii=False, indent=2), encoding="utf-8")
        for group, counter in coverage["by_group"].items():
            self.logger.info("Coverage %s: %s", group, dict(counter))

    # Per-tender collection, shared by all modes.

    async def collect_tender(self, tender_id, directory, tender=None):
        """Write the tender's metadata, notices and files to ``directory``. Return the tender and its documents."""
        tender = tender or await self.get_json(self.TENDER_URL.format(id=tender_id))
        if not isinstance(tender, dict):
            self.logger.warning("No tender %s", tender_id)
            return {}, []
        write_json(directory / "tender.json", tender)

        documents = await self.get_json(self.DOCUMENTS_URL.format(id=tender_id)) or []
        write_json(directory / "documents.json", documents)

        entries = []
        for document in documents:
            entry = {
                "path": None,
                "url": document.get("url"),
                "title": document.get("name"),
                "file_name": document.get("fileName"),
                "published_at": document.get("publishedDate") or document.get("createDate"),
                "role": role(document.get("name"), document.get("fileName")),
                "superseded": document.get("tenderDocumentState") == "Deleted",
                "state": document.get("tenderDocumentState"),
                "delete_reason": document.get("deleteReason"),
            }
            if document.get("fileName") and document.get("tenderDocumentState") != DELETED_AUTOMATICALLY:
                response = await self.fetch(
                    self.DOWNLOAD_URL.format(tender_id=tender_id, doc_id=document["objectId"]), cache=True
                )
                if response.status == HTTPStatus.OK:
                    path = Path(
                        "attachments", UNSAFE_FILENAME_RE.sub("_", f"{document['objectId']}__{document['fileName']}")
                    )
                    write_bytes(directory / path, response.body)
                    entry["path"] = str(path)
                else:
                    entry["error"] = f"HTTP {response.status}"
            entries.append(entry)

        notices = await self.tender_notices(tender)
        write_json(directory / "notices.json", notices)
        for notice in notices:
            number = notice["noticeNumber"]
            response = await self.fetch(self.NOTICE_HTML_URL.format(n=quote(number, safe="")), cache=True)
            entry = {
                "path": None,
                "url": None,
                "title": f"{notice.get('noticeType')} {number}",
                "published_at": notice.get("publicationDate"),
                "role": "notice" if notice.get("noticeType") == "ContractNotice" else "notice_change",
                "superseded": False,
            }
            if response.status == HTTPStatus.OK:
                path = Path("notices", UNSAFE_FILENAME_RE.sub("_", number))
                write_bytes(directory / path.with_suffix(".html"), response.body)
                write_json(directory / path.with_suffix(".json"), _parse_announcement_html(response.text))
                entry["path"] = str(path.with_suffix(".html"))
            entries.append(entry)
        return tender, entries

    async def tender_notices(self, tender):
        """Return the tender's contract notice and notice changes, found among its organization's notices."""
        start = parse_datetime(tender.get("initiationDate"))
        if not start or not tender.get("organizationId") or not tender.get("bzpNumber"):
            return []
        end = max(filter(None, [self.final_deadline(tender), start + dt.timedelta(days=90)]))
        notices = []
        for notice_type in BOARD_NOTICE_TYPES:
            query = {
                "OrganizationId": tender["organizationId"],
                "NoticeType": notice_type,
                "PublicationDateFrom": (start - dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S"),
                "PublicationDateTo": (end + dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S"),
            }
            for page in range(1, MAX_NOTICE_PAGES + 1):
                hits, pagination = await self.board_search({**query, "PageNumber": page}, pagination=True)
                notices.extend(hit for hit in hits or [] if hit.get("tenderId") == tender["objectId"])
                if not pagination.get("HasNext"):
                    break
            else:
                self.logger.warning("Stopped listing %s notices of %s", notice_type, tender["objectId"])
        return sorted(notices, key=lambda notice: notice.get("publicationDate") or "")

    # HTTP helpers.

    def board_search_request(self, query, callback):
        return scrapy.Request(
            self.BOARD_SEARCH_URL.format(query=urlencode({"PageSize": 10, "PageNumber": 1, **query})),
            meta={"dont_cache": True},
            callback=callback,
            cb_kwargs={"query": query},
        )

    async def fetch(self, url_or_request, *, cache=False):
        """Download a URL or request through the downloader middlewares. Return a status-0 response on failure."""
        request = url_or_request
        if isinstance(request, str):
            request = scrapy.Request(request, meta={"dont_cache": not cache}, dont_filter=True)
        try:
            return await self.crawler.engine.download_async(request)
        except Exception as e:  # noqa: BLE001 # download errors are reported per document
            self.logger.warning("Failed %s: %r", request.url, e)
            return scrapy.http.Response(request.url, status=0, request=request)

    async def get_json(self, url, retries=3):
        """Return the JSON body, retrying the server's ``{"error": …}`` responses (usually Elasticsearch timeouts)."""
        for _ in range(retries):
            response = await self.fetch(url)
            if response.status == HTTPStatus.OK:
                data = json.loads(response.body)
                if not (isinstance(data, dict) and "error" in data):
                    return data
            self.logger.debug("Retrying %s (HTTP %s)", url, response.status)
        return None

    async def board_search(self, query, *, pagination=False):
        url = self.BOARD_SEARCH_URL.format(query=urlencode({"PageSize": 10, "PageNumber": 1, **query}))
        for _ in range(3):
            response = await self.fetch(url)
            if response.status == HTTPStatus.OK:
                data = json.loads(response.body)
                if isinstance(data, list):
                    if pagination:
                        return data, json.loads(response.headers.get("X-Pagination", b"{}").decode() or "{}")
                    return data
        return (None, {}) if pagination else None

    async def ted_search(self, query):
        """Return the matching notices, or the HTTP status if the search failed."""
        request = JsonRequest(
            self.TED_SEARCH_URL,
            data={"query": query, "fields": TED_FIELDS, "limit": 100},
            meta={"dont_cache": True},
            dont_filter=True,
        )
        response = await self.fetch(request)
        if response.status != HTTPStatus.OK:
            self.logger.warning("TED search failed (HTTP %s): %s", response.status, query)
            return response.status
        return json.loads(response.body).get("notices") or []


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def write_bytes(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
