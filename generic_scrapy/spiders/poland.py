__review_status__ = "unreviewed"
__generated_by__ = "claude-code/claude-opus-5-5"
__generated_at__ = "2026-11-19"

import asyncio
import collections
import csv
import datetime as dt
import html
import io
import json
import re
import signal
import tarfile
from http import HTTPStatus
from pathlib import Path
from urllib.parse import quote, urlencode

import scrapy
from scrapy.http import JsonRequest

from generic_scrapy.base_spiders.base_spider import BaseSpider
from generic_scrapy.poland.common import (
    HTML_PAGE_ERROR,
    UNSAFE_FILENAME_RE,
    _norm,
    _text,
    compact,
    host,
    parse_datetime,
    role,
    write_bytes,
)
from generic_scrapy.poland.eb2b import EB2B_LIMIT_ERROR, EB2B_LIMIT_RE, EB2B_SLOT, EB2BMixin
from generic_scrapy.poland.logintrade import LogintradeMixin
from generic_scrapy.poland.marketplanet import MARKETPLANET_HOSTS, MarketplanetMixin
from generic_scrapy.poland.smartpzp import SMARTPZP_SLOT, SmartPZPMixin

HTML_RE = re.compile(rb"\s*(?:<!--.*?-->\s*)*<(?:!doctype html|html|head)\b", re.IGNORECASE | re.DOTALL)
JSON_ERROR_RE = re.compile(rb'\s*\{\s*"success"\s*:\s*false\b')
# Matches an h3 prefix in a BZP announcement like "1.5.1.) Ulica" or
# "1.4) Krajowy Numer Identyfikacyjny: REGON 180792174".
# Group 1: dotted key (e.g. "1.5.1"). Group 2: label. Group 3 (optional): post-colon remainder.
ANNOUNCEMENT_KEY_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)*)\.?\)\s*(.*?)(?::\s*(.*))?$", re.DOTALL)
TENDER_ID_RE = re.compile(r"ocds-148610-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
PLATFORM_URL_RE = re.compile(r"Adres strony internetowej prowadzonego postępowania:?\s*(\S+)")
TED_OJS_RE = re.compile(r"(\d{4})/S \d{3}-(\d{6})$")
TED_EFORMS_RE = re.compile(r"\d+-\d{4}$")
URL_DOCUMENT_RE = re.compile(r"<URL_DOCUMENT>\s*([^<\s]+)")
REFERENCE_NUMBER_RE = re.compile(r"<REFERENCE_NUMBER>\s*([^<]+?)\s*<")
# Some buyers copy another procedure's REFERENCE_NUMBER, but give the right one in the title.
TITLE_REFERENCE_RE = re.compile(r"<TITLE>\s*<P>[^<]*?\bnumer postępowania:?\s*([^\s<,;]+)", re.IGNORECASE)
TITLE_RE = re.compile(r"<TITLE>\s*<P>([^<]+)")
# Some buyers append the lot to the procedure's reference in a lot's identifier, e.g. "ZP.371.48.2024.KCCzęść 1".
LOT_SUFFIX_RE = re.compile(r"\s*(?:część|cz\.)\s*\w+$", re.IGNORECASE)
# Documents in this state have no file ("Brak załącznika dla wskazanego dokumentu").
DELETED_AUTOMATICALLY = "DeletedAutomatically"
BOARD_NOTICE_TYPES = ("ContractNotice", "NoticeUpdateNotice")
TED_URL_FIELDS = ("document-url-lot", "document-restricted-url-lot", "submission-url-lot")
TED_FIELDS = [
    "publication-number",
    "publication-date",
    "notice-type",
    "procedure-identifier",
    "internal-identifier-proc",
    "internal-identifier-lot",
    "deadline-receipt-tender-date-lot",
    "deadline-receipt-tender-time-lot",
    "ojs-number",
    "buyer-name",
    "title-proc",
    *TED_URL_FIELDS,
]
# Board/Search ignores PageSize above 10, so cap the pages read when listing an organization's notices.
MAX_NOTICE_PAGES = 30
# The most days before its TED notice that an e-Zamówienia tender is searched for.
TENDER_INITIATION_DAYS = 90
# Platform files that aren't key documents, downloaded last and only up to MAX_MINOR_FILE_SIZE.
MINOR_ROLES = {"annex", "other"}
# Larger annexes are usually design documentation, not the SWZ, OPZ or contract terms.
MAX_MINOR_FILE_SIZE = 100 * 1024 * 1024
# Controls collected at a time, in priority order. They share the per-domain download slots.
CONCURRENT_CONTROLS = 4
PRIORITY_GROUPS = [
    ("cn_test", lambda row: row.get("cn_split") == "test"),
    ("art124_test", lambda row: row.get("art124_split") == "test"),
    ("dev", lambda row: "dev" in {row.get("cn_split"), row.get("art124_split")}),
    ("art124_train", lambda row: row.get("art124_split") == "train"),
    ("cn_aux", lambda row: row.get("cn_split") == "aux"),
]
GROUP_ORDER = {name: i for i, (name, _) in enumerate(PRIORITY_GROUPS)}
STATUSES = ["not_found", "partial", "complete"]
# Hosts misspelled in notices' documents URLs.
HOST_TYPOS = {
    "platformazakupowaz.plk-sa.pl": "platformazakupowa.plk-sa.pl",
    "www.platformazakupowa-plk-sa.pl": "platformazakupowa.plk-sa.pl",
}


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


def first_appeal_at(documents):
    """Return when the first appeal document was published, after which documents can reveal the appeal's grounds."""
    dates = [date for d in documents if d.get("role") == "appeal" and (date := parse_datetime(d.get("published_at")))]
    return min(dates).isoformat() if dates else None


def replaces(row, previous):
    """Return whether a crawled row replaces the manifest's row, which it does unless that row is better or manual."""
    if previous is None:
        return True
    if previous.get("collection") == "manual":
        return False
    return quality(row) >= quality(previous)


def quality(row):
    return STATUSES.index(row["status"]), sum(1 for document in row["documents"] if document.get("path"))


def ted_publication_number(notice):
    """Return the TED API publication number (``NNNNNN-YYYY``) for an OJ S or eForms notice number."""
    if match := TED_OJS_RE.match(notice):
        return f"{int(match.group(2))}-{match.group(1)}"
    if TED_EFORMS_RE.match(notice):
        return notice
    return None


def normalize_url(url):
    """Unescape HTML entities and correct misspelled hosts in a notice's documents URL."""
    url = html.unescape(url)
    for typo, correct in HOST_TYPOS.items():
        url = re.sub(rf"^(?:https?://)?{re.escape(typo)}", f"https://{correct}", url)
    return url


def same_procedure(notice, other):
    """Return whether two TED notices with the same procedure identifier share a reference number or title."""
    reference = notice.get("internal-identifier-proc")
    titles = {compact(title) for title in (notice.get("title-proc") or {}).values()}
    if not reference and not titles:
        return True
    if reference and reference == other.get("internal-identifier-proc"):
        return True
    return bool(titles & {compact(title) for title in (other.get("title-proc") or {}).values()})


def reference_numbers(text):
    """Split a reference like "nr z Platformy: 9090/IREZA3/02597/01189/23/P; nr sprawy: PZ.292.436.2023"."""
    return [number for part in text.split(";") if (number := part.rsplit(":", 1)[-1].strip())]


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


class Poland(MarketplanetMixin, LogintradeMixin, EB2BMixin, SmartPZPMixin, BaseSpider):
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
    ``<FILES_STORE>/<output>/<control_id>/<tenderId>/`` (``output`` defaults to ``poland_tender_documents``), logs
    the crawl's rows to ``runs/<crawl_directory>.jsonl``, and merges them into a ``manifest.json`` (one JSON line per
    control) and a ``coverage.json`` report.
    """

    name = "poland"

    # BaseSpider
    default_from_date = "2021-01-01"

    SEARCH_URL = (
        "https://ezamowienia.gov.pl/mp-readmodels/api/Search/SearchTenders"
        "?Page={page}&PageSize=100&SortingColumnName=InitiationDate&SortingDirection=DESC"
    )
    ORGANIZATION_SEARCH_URL = SEARCH_URL.format(page=1) + "&{query}"
    BOARD_SEARCH_URL = "https://ezamowienia.gov.pl/mo-board/api/v1/Board/Search?{query}"
    TENDER_URL = "https://ezamowienia.gov.pl/mp-readmodels/api/Search/GetTender?id={id}"
    DOCUMENTS_URL = "https://ezamowienia.gov.pl/mp-readmodels/api/Search/GetTenderDocuments?tenderId={id}"
    DOWNLOAD_URL = "https://ezamowienia.gov.pl/mp-readmodels/api/Tender/DownloadDocument/{tender_id}/{doc_id}"
    NOTICE_HTML_URL = "https://ezamowienia.gov.pl/mo-board/api/v1/Board/GetNoticeHtmlBody?noticeNumber={n}"
    PROCEDURE_URL = "https://ezamowienia.gov.pl/mp-client/search/list/{id}"
    TED_SEARCH_URL = "https://api.ted.europa.eu/v3/notices/search"
    TED_XML_URL = "https://ted.europa.eu/en/notice/{n}/xml"
    TED_PACKAGE_URL = "https://ted.europa.eu/packages/daily/{year}{issue:05d}"
    TED_NOTICE_URL = "https://ted.europa.eu/en/notice/-/detail/{n}"
    TED_PDF_URL = "https://ted.europa.eu/pl/notice/{n}/pdf"

    custom_settings = {
        "CONCURRENT_REQUESTS_PER_DOMAIN": 2,
        "DOWNLOAD_DELAY": 0.5,
        # The TED Search API answers bursts with HTTP 429, and ted.europa.eu with a WAF challenge.
        "DOWNLOAD_SLOTS": {
            "api.ted.europa.eu": {"concurrency": 1, "delay": 2},
            "ted.europa.eu": {"concurrency": 1, "delay": 3},
            # eB2B limits downloads in a short time across its subdomains (see eb2b_request).
            EB2B_SLOT: {"concurrency": 1, "delay": 5},
            SMARTPZP_SLOT: {"concurrency": 1, "delay": 2},
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

    def __init__(self, *args, notice_numbers=None, priority=None, platforms=None, output=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.notice_numbers = notice_numbers
        self.output = output or "poland_tender_documents"
        self.priority = priority.split(",") if priority else None
        # Platform adapters are opt-in. Check a platform's terms of use before collecting from it.
        self.platforms = set(platforms.split(",")) if platforms else set()
        self.manifest = []
        self.manifest_file = None
        self.draining = False
        self.control_slots = asyncio.Semaphore(CONCURRENT_CONTROLS)
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

    def output_directory(self):
        return Path(self.settings["FILES_STORE"], self.output)

    def control_requests(self):
        directory = self.output_directory() / "runs"
        directory.mkdir(parents=True, exist_ok=True)
        # Log each control's row when it finishes, so that a crash or forced stop keeps finished controls.
        self.manifest_file = (directory / f"{self.crawl_directory}.jsonl").open("a", encoding="utf-8")
        signal.signal(signal.SIGUSR1, self.drain)

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

    def drain(self, _signum, _frame):
        """Skip the controls not yet started, so the crawl closes once the controls in progress finish."""
        self.logger.info("Received SIGUSR1, finishing the controls in progress")
        self.draining = True

    async def parse_control(self, _response, control):
        # The semaphore's waiters are first-in, first-out, so controls start in priority order.
        async with self.control_slots:
            if not self.draining:
                await self.collect_control(control)

    async def collect_control(self, control):
        directory = self.output_directory() / control["control_id"]
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
            "collection": "crawl",
            "crawl": self.crawl_directory,
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
        urls = list(dict.fromkeys(u for r in resolutions for u in r.get("urls", [])))
        platform = {}
        references = list(dict.fromkeys(ref for r in resolutions for ref in r.get("references", [])))
        if not tender_ids and urls and "marketplanet" in self.platforms and host(urls[0]).endswith(MARKETPLANET_HOSTS):
            resolution = next((r for r in resolutions if r.get("title") and r.get("published")), None)
            platform = await self.collect_marketplanet(urls[0], references, directory, resolution)
        elif not tender_ids and urls and "logintrade" in self.platforms and host(urls[0]).endswith("logintrade.net"):
            other_urls = [u for r in resolutions for u in r.get("other_urls", [])]
            platform = await self.collect_logintrade(urls[0], references, directory, other_urls)
        elif not tender_ids and urls and "eb2b" in self.platforms and host(urls[0]).endswith("eb2b.com.pl"):
            platform = await self.collect_eb2b(urls[0], references, directory)
        elif not tender_ids and urls and "smartpzp" in self.platforms and host(urls[0]) == SMARTPZP_SLOT:
            platform = await self.collect_smartpzp(urls[0], references, directory)
        if platform:
            row["documents"].extend(platform.get("documents", []))
            if platform.get("deadline"):
                row["bid_deadline_final"] = platform["deadline"].isoformat()
        notices = self.ted_documents(ted_notices.values())
        if not tender_ids:
            notices.extend(self.bzp_documents(resolutions, directory))
        row["documents"].extend(notices)
        if not row["bid_deadline_final"] and len(deadlines) > 1:
            row["bid_deadline_final"] = deadlines[-1].isoformat()
        row["bid_deadline_final"] = row["bid_deadline_final"] or row["bid_deadline_original"]

        if final := parse_datetime(row["bid_deadline_final"]):
            for document in row["documents"]:
                if published := parse_datetime(document.get("published_at")):
                    document["after_deadline"] = published > final
        row["first_appeal_at"] = first_appeal_at(row["documents"])
        await self.save_ted_notices(notices, directory)

        # A link to another site has no file name, but counts as a file that wasn't downloaded. Notices alone don't
        # make a procedure's documents found.
        files = [d for d in row["documents"] if (d.get("file_name") or d.get("error")) and d not in notices]
        saved = [d for d in files if d.get("path")]
        if tender_ids:
            row["source"] = "ezamowienia"
            row["procedure_url"] = self.PROCEDURE_URL.format(id=tender_ids[0])
            deleted = sum(d.get("state") == DELETED_AUTOMATICALLY for d in files)
            row["reason"] = f"e-Zamówienia deleted the files ({deleted} of {len(files)} deleted automatically)"
        elif urls:
            row["source"] = f"platform:{host(urls[0])}"
            row["procedure_url"] = next(iter(platform.get("urls", [])), urls[0])
            errors = collections.Counter(d["error"] for d in files if d.get("error"))
            if files and errors:
                row["reason"] = "no file downloaded: " + "; ".join(f"{e} ({n})" for e, n in errors.most_common())
            else:
                row["reason"] = platform.get("error") or (
                    "no files on the procedure page"
                    if platform
                    else "no tender in progress on e-Zamówienia with the notice's number or title"
                    if host(urls[0]) == "ezamowienia.gov.pl"
                    else "no adapter for the platform"
                )
        else:
            row["reason"] = "; ".join(r["error"] for r in resolutions if r.get("error")) or "no documents URL"
        if saved:
            row["status"] = "complete" if len(saved) == len(files) else "partial"
            row["reason"] = (
                None if len(saved) == len(files) else f"{len(files) - len(saved)} of {len(files)} files unavailable"
            )

        self.manifest.append(row)
        self.manifest_file.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.manifest_file.flush()

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
        if hit.get("noticeType") == "AgreementIntentionNotice":
            return {
                "error": f"{notice}: single-source award (notice of intention to conclude a contract), "
                "so no tender documents"
            }
        result = {"deadline": parse_datetime(hit.get("submittingOffersDate"))}
        if hit.get("tenderId"):
            tender = await self.get_json(self.TENDER_URL.format(id=hit["tenderId"]))
            if isinstance(tender, dict) and tender.get("isProceedingOnPlatform"):
                return {**result, "tender_id": hit["tenderId"]}
        response = await self.fetch(self.NOTICE_HTML_URL.format(n=quote(hit["noticeNumber"], safe="")))
        if response.status != HTTPStatus.OK:
            return {**result, "error": f"{notice}: notice body unavailable (HTTP {response.status})"}
        result["bzp_notice"] = {"hit": hit, "body": response.body}
        items = (item for section in _parse_announcement_html(response.text)["sections"] for item in section["items"])
        result["references"] = [
            item["value"] for item in items if item["label"] == "Numer referencyjny" and item["value"]
        ]
        if match := PLATFORM_URL_RE.search(_norm(" ".join(response.xpath("//text()").getall()))):
            return {**result, "urls": [normalize_url(match.group(1))]}
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
                # Some buyers reuse a procedure identifier across procedures.
                notices = [n for n in procedure_notices if same_procedure(notice, n)] or notices
        # Later notices can correct the reference number.
        latest = max(
            (n for n in notices if n.get("internal-identifier-proc") and n.get("notice-type", "").startswith("cn")),
            key=lambda n: n.get("publication-date") or "",
            default=notice,
        )
        result = {
            "ted_notices": notices,
            "deadline": self.procedure_deadline(notice, notices),
            # Fall back to the lots' identifiers, which some buyers use as the procedure's.
            "references": (
                reference_numbers(latest["internal-identifier-proc"])
                if latest.get("internal-identifier-proc")
                else list(
                    dict.fromkeys(LOT_SUFFIX_RE.sub("", ref) for ref in notice.get("internal-identifier-lot") or [])
                )
            ),
            "title": next(iter((notice.get("title-proc") or {}).values()), None),
            "published": parse_datetime(notice.get("publication-date")),
        }

        urls = next(([u for u in notice.get(field) or [] if u] for field in TED_URL_FIELDS if notice.get(field)), [])
        if not urls:
            xml, error = await self.ted_notice_xml(publication_number, notice)
            if error:
                return {**result, "error": f"{publication_number}: {error}"}
            if xml:
                urls = URL_DOCUMENT_RE.findall(xml)
                if match := TITLE_REFERENCE_RE.search(xml) or REFERENCE_NUMBER_RE.search(xml):
                    result["references"] = reference_numbers(match.group(1))
                if match := TITLE_RE.search(xml):
                    result["title"] = html.unescape(match.group(1))
        urls = [normalize_url(url) for url in urls]
        for url in urls:
            if match := TENDER_ID_RE.search(url):
                return {**result, "tender_id": match.group(0)}
        if any(host(url) == "ezamowienia.gov.pl" for url in urls) and (
            tender_id := await self.ezamowienia_tender_id(publication_number, notice)
        ):
            return {**result, "tender_id": tender_id}
        result["other_urls"] = [normalize_url(u) for field in TED_URL_FIELDS for u in notice.get(field) or [] if u]
        return (
            {**result, "urls": list(dict.fromkeys(urls))}
            if urls
            else {**result, "error": f"{publication_number}: no documents URL"}
        )

    async def ezamowienia_tender_id(self, publication_number, notice):
        """Return the ID of the e-Zamówienia tender by the notice's buyer that the TED notice announces."""
        number, _, year = publication_number.partition("-")
        # Buyers enter the TED number in any format, e.g. "2023/S 163-514701" or "2026-OJS207-00738097-pl-ts".
        number_re = re.compile(rf"(?<!\d)0*{int(number)}(?!\d)")
        titles = {compact(title) for title in (notice.get("title-proc") or {}).values()}
        query = {}
        if published := parse_datetime(notice.get("publication-date")):
            # Without a date range, the search returns only recent tenders.
            query["InitiationDateFrom"] = (published - dt.timedelta(days=TENDER_INITIATION_DAYS)).date().isoformat()
            query["InitiationDateTo"] = (published + dt.timedelta(days=1)).date().isoformat()
        for name in dict.fromkeys(name for names in (notice.get("buyer-name") or {}).values() for name in names):
            tenders = await self.get_json(
                self.ORGANIZATION_SEARCH_URL.format(query=urlencode({"OrganizationName": name, **query}))
            )
            tenders = tenders if isinstance(tenders, list) else []
            for tender in tenders:
                ted = tender.get("tedContractNoticeNumber") or ""
                if (number_re.search(ted) and year in ted) or compact(tender.get("title")) in titles:
                    return tender["objectId"]
            # Old TED notices have no procedure title. Accept the buyer's only tender without a TED number that week.
            if published and not titles:
                week = (published - dt.timedelta(days=7)).date().isoformat()
                candidates = [
                    t for t in tenders if not t.get("tedContractNoticeNumber") and t.get("initiationDate", "") >= week
                ]
                if len(candidates) == 1:
                    return candidates[0]["objectId"]
        return None

    async def ted_notice_xml(self, publication_number, notice):
        """Return the notice's XML and an error. If the WAF blocks it, read it from its OJ S issue's daily package."""
        response = await self.fetch(self.TED_XML_URL.format(n=publication_number))
        if response.status == HTTPStatus.OK:
            return response.text, None
        if response.status != HTTPStatus.ACCEPTED:
            return None, None
        issue, _, year = (notice.get("ojs-number") or "").partition("/")
        if not issue.isdigit():
            return None, "TED notice XML blocked (HTTP 202), and no OJ S issue"
        package = await self.fetch(self.TED_PACKAGE_URL.format(year=year, issue=int(issue)), cache=True)
        if package.status != HTTPStatus.OK:
            return None, f"TED notice XML blocked (HTTP 202), and daily package unavailable (HTTP {package.status})"
        number, _, year = publication_number.partition("-")
        # Packages name files like 00605630_2023.xml, or 568344_2022.xml before 2023.
        name = f"{int(number)}_{year}.xml"
        # Read the member in memory: the package is untrusted, so nothing is extracted to disk.
        with tarfile.open(fileobj=io.BytesIO(package.body), mode="r:gz") as archive:
            for member in archive:
                if member.isfile() and Path(member.name).name.lstrip("0") == name:
                    return archive.extractfile(member).read().decode(), None
        return None, f"TED notice XML blocked (HTTP 202), and not in daily package {year}/{issue}"

    @staticmethod
    def ted_deadline(notice):
        times = notice.get("deadline-receipt-tender-time-lot") or []
        deadlines = [
            parse_datetime(f"{date[:10]}T{times[i]}" if i < len(times) and times[i] else date)
            for i, date in enumerate(notice.get("deadline-receipt-tender-date-lot") or [])
        ]
        return max(filter(None, deadlines), default=None)

    @classmethod
    def procedure_deadline(cls, notice, notices):
        """Return the notice's bid deadline, as moved by the procedure's later notices published before it."""
        deadline = cls.ted_deadline(notice)
        start = notice.get("publication-date") or ""
        for other in sorted(notices, key=lambda n: n.get("publication-date") or ""):
            published = (other.get("publication-date") or "")[:10]
            if published <= start[:10] or not deadline:
                continue
            if published > deadline.date().isoformat():
                break
            deadline = cls.ted_deadline(other) or deadline
        return deadline

    @staticmethod
    def bzp_documents(resolutions, directory):
        """Save the BZP notices of a procedure run on another platform, and return their documents."""
        documents = []
        for resolution in resolutions:
            if not (notice := resolution.get("bzp_notice")):
                continue
            number = notice["hit"]["noticeNumber"]
            path = Path("notices", UNSAFE_FILENAME_RE.sub("_", number) + ".html")
            write_bytes(directory / path, notice["body"])
            documents.append(
                {
                    "path": str(Path(directory.name, path)),
                    "url": None,
                    "title": f"{notice['hit'].get('noticeType')} {number}",
                    "file_name": path.name,
                    "published_at": notice["hit"].get("publicationDate"),
                    "role": "notice" if notice["hit"].get("noticeType") == "ContractNotice" else "notice_change",
                    "superseded": False,
                }
            )
        return documents

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

    async def save_ted_notices(self, documents, directory):
        """Save the Polish PDF of each TED notice published before the bid deadline."""
        for document in documents:
            if document["path"] or document.get("after_deadline"):
                continue
            number = document["url"].rsplit("/", 1)[1]
            document["file_name"] = f"{number}.pdf"
            response = await self.fetch(self.TED_PDF_URL.format(n=number), cache=True)
            if response.status != HTTPStatus.OK:
                document["error"] = f"HTTP {response.status}"
            elif not response.body.startswith(b"%PDF"):
                document["error"] = HTML_PAGE_ERROR
            else:
                path = Path("ted", document["file_name"])
                write_bytes(directory / path, response.body)
                document["path"] = str(Path(directory.name, path))

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
        if not self.manifest_file:
            return
        self.manifest_file.close()
        directory = self.output_directory()
        rows = {}
        if (directory / "manifest.json").exists():
            with (directory / "manifest.json").open(encoding="utf-8") as f:
                rows = {row["control_id"]: row for row in map(json.loads, f)}
        for row in self.manifest:
            if replaces(row, rows.get(row["control_id"])):
                rows[row["control_id"]] = row
        manifest = sorted(
            rows.values(), key=lambda row: (GROUP_ORDER.get(row["group"], len(GROUP_ORDER)), row["control_id"])
        )
        with (directory / "manifest.json").open("w", encoding="utf-8") as f:
            f.writelines(json.dumps(row, ensure_ascii=False) + "\n" for row in manifest)

        counts = collections.defaultdict(collections.Counter)
        for row in manifest:
            counts[row["group"]][row["status"]] += 1
        for control in self.skipped:
            counts[control["group"]]["skipped"] += 1
        coverage = {
            "by_group": {group: dict(counter) for group, counter in counts.items()},
            "by_source": dict(collections.Counter(row["source"] for row in manifest).most_common()),
            "not_found": [
                {key: row[key] for key in ("control_id", "group", "notice_ids", "source", "procedure_url", "reason")}
                for row in manifest
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
            elif document.get("url"):
                entry["error"] = "link to another site, not followed"
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

    # Platform adapters, for the notice_numbers mode.

    async def download_files(self, directory, files):
        """Download each ``(entry, path, request)``, key documents first, and set the entry's path or error."""
        for entry, path, request in sorted(files, key=lambda file: file[0]["role"] in MINOR_ROLES):
            # Re-runs with the same crawl_time resume, as Marketplanet downloads aren't cached.
            if not (directory / path).exists():
                if entry["role"] in MINOR_ROLES and (entry["size"] or 0) > MAX_MINOR_FILE_SIZE:
                    entry["error"] = "not downloaded (annex or other file over 100 MB)"
                    continue
                # Some files are hundreds of MB, and platforms can send them slowly.
                request.meta.update({"download_timeout": 1800, "max_retry_times": 1})
                response = await self.fetch_file(request)
                if response.status != HTTPStatus.OK:
                    entry["error"] = f"HTTP {response.status}"
                    continue
                # Platforms answer missing files, expired links and blocks with an HTML page. Check the body, not
                # the Content-Type, which some servers set to text/html for real documents.
                if HTML_RE.match(response.body[:512]) and not path.suffix.lower().startswith(".htm"):
                    entry["error"] = HTML_PAGE_ERROR
                    continue
                if JSON_ERROR_RE.match(response.body[:512]) and path.suffix.lower() != ".json":
                    message = json.loads(response.body).get("msg") or ""
                    entry["error"] = (
                        EB2B_LIMIT_ERROR if EB2B_LIMIT_RE.search(message) else f"platform error: {message}"
                    )
                    continue
                write_bytes(directory / path, response.body)
            entry["path"] = str(Path(directory.name, path))

    # HTTP helpers.

    async def fetch_file(self, request):
        """Download a file, waiting out eB2B's download limit (about 50 files an hour), unless draining."""
        while True:
            response = await self.fetch(request.copy())
            if response.status != HTTPStatus.OK or not JSON_ERROR_RE.match(response.body[:512]):
                return response
            match = EB2B_LIMIT_RE.search(json.loads(response.body).get("msg") or "")
            if not match or self.draining:
                return response
            minutes, seconds = int(match.group(1) or 0), int(match.group(2) or 0)
            self.logger.info("eB2B download limit: waiting %d min for %s", minutes + 1, request.url)
            await asyncio.sleep(minutes * 60 + seconds + 5)

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
