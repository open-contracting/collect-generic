__review_status__ = "unreviewed"
__generated_by__ = "claude-code/claude-opus-5-5"
__generated_at__ = "2026-11-24"

import asyncio
import collections
import csv
import datetime as dt
import json
import re
import signal
from http import HTTPStatus
from pathlib import Path

import scrapy

from generic_scrapy.base_spiders.base_spider import BaseSpider
from generic_scrapy.poland.common import (
    HTML_PAGE_ERROR,
    HTML_RE,
    TENDER_ID_RE,
    describe_file,
    host,
    parse_datetime,
    require_store,
    same_buyer,
    skippable,
    text_layer,
    write_bytes,
    zip_directory,
    zip_members,
)
from generic_scrapy.poland.eb2b import EB2B_HOSTS, EB2B_LIMIT_ERROR, EB2B_LIMIT_RE, EB2B_SLOT, EB2BMixin
from generic_scrapy.poland.ezamowienia import DELETED_AUTOMATICALLY, EZamowieniaMixin
from generic_scrapy.poland.josephine import JOSEPHINE_HOST, JosephineMixin
from generic_scrapy.poland.logintrade import LogintradeMixin
from generic_scrapy.poland.marketplanet import MARKETPLANET_HOSTS, MarketplanetMixin
from generic_scrapy.poland.smartpzp import SMARTPZP_SLOT, SmartPZPMixin
from generic_scrapy.poland.ted import TEDMixin, ted_publication_number
from generic_scrapy.poland.zeto import ZETO_HOSTS, ZETO_OFFER_RE, ZetoMixin

JSON_ERROR_RE = re.compile(rb'\s*\{\s*"success"\s*:\s*false\b')
# Documents published after the bid deadline, or on its day for the opening.
POST_DEADLINE_RE = re.compile(r"wyb[oó]r|wynik|udzieleni|uniewa|cofni|\(can-|\(veat")
OPENING_RE = re.compile(r"otwarci|kwo[tc]|zestawieni\w* ofert|art\.? ?222")
# Platform files downloaded after the SWZ, OPZ, contract terms, changes and answers.
MINOR_ROLES = {"annex", "other"}
# Design documentation and bills of quantities over this size are recorded but not downloaded (see skippable()).
SKIPPABLE_FILE_SIZE = 100 * 1024 * 1024
# Some OPZ archives are several hundred MB.
MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024
# PKP PLK's platform cuts downloads at random points and ignores Range requests, so larger files rarely complete.
HOST_MAX_FILE_SIZE = {"platformazakupowa.plk-sa.pl": 200 * 1024 * 1024}
# The end of a ZIP file, which holds its member list unless the archive has thousands of members.
ZIP_TAIL_SIZE = 64 * 1024
# Controls collected at a time, in priority order. They share the per-domain download slots.
CONCURRENT_CONTROLS = 8
PRIORITY_GROUPS = [
    ("cn_test", lambda row: row.get("cn_split") == "test"),
    ("art124_test", lambda row: row.get("art124_split") == "test"),
    ("dev", lambda row: "dev" in {row.get("cn_split"), row.get("art124_split")}),
    ("art124_train", lambda row: row.get("art124_split") == "train"),
    ("cn_aux", lambda row: row.get("cn_split") == "aux"),
]
GROUP_ORDER = {name: i for i, (name, _) in enumerate(PRIORITY_GROUPS)}


def first_appeal_at(documents):
    """Return when the first appeal document was published, after which documents can reveal the appeal's grounds."""
    dates = [date for d in documents if d.get("role") == "appeal" and (date := parse_datetime(d.get("published_at")))]
    return min(dates).isoformat() if dates else None


def extends(document, deadline):
    """Return whether the document, published after the deadline, shows that the deadline was extended."""
    published = parse_datetime(document.get("published_at"))
    if not published or published <= deadline or document.get("role") == "appeal":
        return False
    text = f"{document.get('title') or ''} {document.get('file_name') or ''}".lower().replace("_", " ")
    if POST_DEADLINE_RE.search(text):
        return False
    return not (OPENING_RE.search(text) and published.date() == deadline.date())


def merge(row, previous):
    """
    Return the crawled row, with the stored documents of the manifest's row that the crawl didn't store.

    Return the manifest's row instead if it was collected manually, or if the crawl stored nothing but it did.
    """
    if previous is None:
        return row
    if previous.get("collection") == "manual" or (stored(previous) and not stored(row)):
        return previous

    def identity(document):
        return document.get("title"), document.get("file_name")

    # A procedure can list several files with the same name.
    old = collections.defaultdict(list)
    for document in previous["documents"]:
        if document.get("path"):
            old[identity(document)].append(document)
    documents = []
    filled = collections.Counter()
    for document in row["documents"]:
        if not document.get("path") and old[identity(document)]:
            documents.append(old[identity(document)].pop(0))
            filled[document.get("role"), document.get("title")] += 1
        else:
            if old[identity(document)]:
                old[identity(document)].pop(0)
            documents.append(document)
    # Documents that the platform no longer lists.
    documents.extend(d for remaining in old.values() for d in remaining)

    merged = {**row, "documents": documents}
    merged["files_skipped"] = dict(collections.Counter(d["skipped"] for d in documents if d.get("skipped")))
    unavailable = []
    for document in row["key_unavailable"]:
        if filled[document["role"], document["title"]]:
            filled[document["role"], document["title"]] -= 1
        else:
            unavailable.append(document)
    merged["key_unavailable"] = unavailable
    if row["status"] == "partial" and not unavailable:
        merged["status"], merged["reason"] = "complete", None
    elif row["status"] == "partial" and (match := re.fullmatch(r"\d+ of (\d+) key files unavailable", row["reason"])):
        merged["reason"] = f"{len(unavailable)} of {match.group(1)} key files unavailable"
    return merged


def different_buyers(control, resolutions):
    """Return why the control's notices aren't one buyer's (or the input's ``buyer``'s), or None."""
    named = [
        (notice, r["buyers"]) for notice, r in zip(control["notices"], resolutions, strict=True) if r.get("buyers")
    ]
    if control["buyer"]:
        named.insert(0, ("input", [control["buyer"]]))
    if all(same_buyer(named[0][1], buyers) for _, buyers in named[1:]):
        return None
    return "notices from different buyers: " + "; ".join(f"{'; '.join(b)} ({notice})" for notice, b in named)


def set_status(row, files):
    """Set the status from the files other than notices, if any is saved. Files are key unless ``skipped``."""
    key = [d for d in files if not d.get("skipped")]
    unavailable = [d for d in key if not d.get("path") and not d.get("after_deadline")]
    row["files_skipped"] = dict(collections.Counter(d["skipped"] for d in files if d.get("skipped")))
    row["key_unavailable"] = [{"role": d["role"], "title": d["title"]} for d in unavailable]
    if any(d.get("path") for d in key):
        row["status"] = "partial" if unavailable else "complete"
        row["reason"] = f"{len(unavailable)} of {len(key)} key files unavailable" if unavailable else None


def stored(row):
    return any(document.get("path") for document in row["documents"])


class Poland(
    TEDMixin,
    EZamowieniaMixin,
    MarketplanetMixin,
    LogintradeMixin,
    EB2BMixin,
    SmartPZPMixin,
    ZetoMixin,
    JosephineMixin,
    BaseSpider,
):
    """
    Per-procedure file collector for the Polish public-procurement portal (ezamowienia.gov.pl).

    The e-Zamówienia and TED code and the platform adapters are mixins in generic_scrapy/poland/.

    Entry modes:

    - Default: walks the SearchTenders catalogue (``Initiated`` tenders only).
    - ``-a from_date=YYYY-MM-DD -a until_date=YYYY-MM-DD``: walks contract notices in Board/Search by publication
      date, both days included, which reaches closed tenders too.
    - ``-a notice_numbers=<csv>``: resolves each control's BZP or TED notices (or tender IDs or documents URLs) to its
      procedure, for the oecd-poland control analysis. ``-a priority=cn_test,art124_test`` selects priority groups.

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

    custom_settings = {
        "DOWNLOADER_MIDDLEWARES": {
            # After UserAgentMiddleware (400).
            "generic_scrapy.poland.common.BrowserUserAgentMiddleware": 450,
        },
        "CONCURRENT_REQUESTS_PER_DOMAIN": 2,
        "DOWNLOAD_DELAY": 0.5,
        # The TED Search API answers bursts with HTTP 429, and ted.europa.eu with a WAF challenge. TED's fair usage
        # policy allows 700 requests a minute, and 600 notice views or downloads in 6 minutes.
        # https://ted.europa.eu/en/fair-usage-policy-on-ted
        "DOWNLOAD_SLOTS": {
            "api.ted.europa.eu": {"concurrency": 1, "delay": 1},
            "ted.europa.eu": {"concurrency": 1, "delay": 2},
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

    def __init__(
        self, *args, notice_numbers=None, priority=None, platforms=None, output=None, skip_large=None, **kwargs
    ):
        super().__init__(*args, **kwargs)
        self.notice_numbers = notice_numbers
        self.output = output or "poland_tender_documents"
        self.priority = priority.split(",") if priority else None
        self.skip_large = skip_large != "false"
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
        self.output_directory()
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

    # notice_numbers mode.

    def output_directory(self):
        return require_store(Path(self.settings["FILES_STORE"], self.output))

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
            control = {
                "control_id": row["control_id"],
                "group": group,
                "kind": row.get("kind"),
                "notices": notices,
                "buyer": row.get("buyer"),
            }
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
        row["contract_type"] = next((r["contract_type"] for r in resolutions if r.get("contract_type")), None)
        if reason := different_buyers(control, resolutions):
            row["reason"] = reason
            self.log_row(row)
            return
        tender_ids = list(dict.fromkeys(r["tender_id"] for r in resolutions if r.get("tender_id")))
        ted_notices = {n["publication-number"]: n for r in resolutions for n in r.get("ted_notices", [])}
        deadlines = sorted(filter(None, (r.get("deadline") for r in resolutions)))
        originals = sorted(filter(None, (r.get("original_deadline") or r.get("deadline") for r in resolutions)))
        row["bid_deadline_original"] = originals[0].isoformat() if originals else None

        for tender_id in tender_ids:
            tender, documents = await self.collect_tender(tender_id, directory / tender_id)
            for document in documents:
                if document.get("path"):
                    document["path"] = str(Path(control["control_id"], tender_id, document["path"]))
            row["documents"].extend(documents)
            row["tender_ids"].append(tender_id)
            if deadline := self.tender_final_deadline(tender):
                row["bid_deadline_final"] = max(filter(None, [row["bid_deadline_final"], deadline.isoformat()]))
            if not row["bid_deadline_original"] and (first := self.tender_first_deadline(tender)):
                row["bid_deadline_original"] = first.isoformat()
        urls = list(dict.fromkeys(u for r in resolutions for u in r.get("urls", [])))
        platform = {}
        references = list(dict.fromkeys(ref for r in resolutions for ref in r.get("references", [])))
        if not tender_ids and urls and "marketplanet" in self.platforms and host(urls[0]).endswith(MARKETPLANET_HOSTS):
            resolution = next((r for r in resolutions if r.get("title") and r.get("published")), None)
            platform = await self.collect_marketplanet(urls[0], references, directory, resolution)
        elif not tender_ids and urls and "logintrade" in self.platforms and host(urls[0]).endswith("logintrade.net"):
            other_urls = [u for r in resolutions for u in r.get("other_urls", [])]
            platform = await self.collect_logintrade(urls[0], references, directory, other_urls)
        elif not tender_ids and urls and "eb2b" in self.platforms and host(urls[0]).endswith(EB2B_HOSTS):
            platform = await self.collect_eb2b(urls[0], references, directory)
        elif not tender_ids and urls and "smartpzp" in self.platforms and host(urls[0]) == SMARTPZP_SLOT:
            platform = await self.collect_smartpzp(urls[0], references, directory)
        elif not tender_ids and urls and "zeto" in self.platforms and host(urls[0]) in ZETO_HOSTS:
            resolution = next((r for r in resolutions if r.get("title")), None)
            url = next((u for u in urls if ZETO_OFFER_RE.search(u)), urls[0])
            platform = await self.collect_zeto(url, references, directory, resolution)
        elif not tender_ids and urls and "josephine" in self.platforms and host(urls[0]) == JOSEPHINE_HOST:
            other_urls = [u for r in resolutions for u in r.get("other_urls", [])]
            platform = await self.collect_josephine(urls[0], references, directory, other_urls)
        if platform:
            row["documents"].extend(platform.get("documents", []))
            if platform.get("deadline"):
                row["bid_deadline_final"] = platform["deadline"].isoformat()
        notices = self.ted_documents(ted_notices.values())
        if not tender_ids:
            notices.extend(self.bzp_documents(resolutions, directory))
        row["documents"].extend(notices)
        if not row["bid_deadline_final"]:
            # A buyer can extend the deadline without a TED notice, so a later document can show a later deadline.
            notice_deadline = deadlines[-1] if deadlines else parse_datetime(row["bid_deadline_original"])
            if notice_deadline and not any(extends(d, notice_deadline) for d in row["documents"]):
                row["bid_deadline_final"] = notice_deadline.isoformat()

        if final := parse_datetime(row["bid_deadline_final"]):
            for document in row["documents"]:
                if published := parse_datetime(document.get("published_at")):
                    document["after_deadline"] = published > final
        row["first_appeal_at"] = first_appeal_at(row["documents"])
        await self.save_ted_notices(notices, directory)

        # A link to another site has no file name, but counts as a file that wasn't downloaded. Notices alone don't
        # make a procedure's documents found.
        files = [
            d
            for d in row["documents"]
            if (d.get("file_name") or d.get("error") or "skipped" in d) and d not in notices
        ]
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
        for document in row["documents"]:
            if document.get("path"):
                describe_file(document, self.output_directory() / document["path"])
        set_status(row, files)

        self.log_row(row)

    def log_row(self, row):
        self.manifest.append(row)
        self.manifest_file.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.manifest_file.flush()

    async def resolve(self, notice):
        """Resolve a notice number to a tender ID or documents URL, with its notices and bid deadline."""
        if publication_number := ted_publication_number(notice):
            return await self.resolve_ted(publication_number)
        if "/BZP " in notice:
            return await self.resolve_bzp(notice)
        # A tender ID or documents URL, for a procedure that its notices don't lead to.
        if TENDER_ID_RE.fullmatch(notice):
            return {"tender_id": notice}
        if notice.startswith("https://"):
            return {"urls": [notice]}
        return {"error": f"{notice}: unsupported notice number"}

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
            rows[row["control_id"]] = merge(row, rows.get(row["control_id"]))
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

    # Platform adapters, for the notice_numbers mode.

    async def download_files(self, directory, files):
        """Download each ``(entry, path, request)``, key documents first, and set the entry's path or error."""
        for entry, path, request in sorted(files, key=lambda file: file[0]["role"] in MINOR_ROLES):
            # Re-runs with the same crawl_time resume, as Marketplanet downloads aren't cached.
            if not (directory / path).exists():
                size = entry["size"] or 0
                if size > SKIPPABLE_FILE_SIZE and (category := skippable(entry["title"], entry.get("file_name"))):
                    # Kept if the download fails, so that the status doesn't depend on skip_large.
                    entry["skipped"] = category
                    if self.skip_large:
                        if path.suffix.lower() == ".zip":
                            entry["contents"] = await self.zip_contents(request, size)
                        continue
                if size > MAX_FILE_SIZE:
                    entry["error"] = "not downloaded (over 2 GB)"
                    continue
                if size > (limit := HOST_MAX_FILE_SIZE.get(host(request.url), MAX_FILE_SIZE)):
                    entry["error"] = f"not downloaded (over {limit // 1024 // 1024} MB on this platform)"
                    continue
                # Some files are hundreds of MB, and platforms can send them slowly.
                request.meta.update(
                    {"download_timeout": 1800, "max_retry_times": 1, "download_maxsize": MAX_FILE_SIZE}
                )
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
            entry.pop("skipped", None)
            if path.suffix.lower() == ".pdf" and "text_layer" not in entry:
                entry["text_layer"] = text_layer((directory / path).read_bytes())
            entry["path"] = str(Path(directory.name, path))

    async def zip_contents(self, request, size):
        """Return ``[name, size]`` for each member of a ZIP file, from two small range requests, or None."""
        meta = {"dont_cache": True, "download_maxsize": 2 * ZIP_TAIL_SIZE, "max_retry_times": 1}
        response = await self.fetch_file(
            request.replace(
                headers={**request.headers.to_unicode_dict(), "Range": f"bytes=-{ZIP_TAIL_SIZE}"},
                meta={**request.meta, **meta},
            )
        )
        if response.status != HTTPStatus.PARTIAL_CONTENT or not (location := zip_directory(response.body)):
            return None
        offset, length = location
        start = offset - (size - len(response.body))
        if start >= 0:
            return zip_members(response.body[start : start + length]) or None
        meta["download_maxsize"] = length + 1024
        response = await self.fetch_file(
            request.replace(
                headers={**request.headers.to_unicode_dict(), "Range": f"bytes={offset}-{offset + length - 1}"},
                meta={**request.meta, **meta},
            )
        )
        if response.status != HTTPStatus.PARTIAL_CONTENT:
            return None
        return zip_members(response.body) or None

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
            # Free the control's slot while waiting, so that other controls can start.
            self.control_slots.release()
            try:
                await asyncio.sleep(minutes * 60 + seconds + 5)
            finally:
                await self.control_slots.acquire()

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
