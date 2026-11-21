__review_status__ = "unreviewed"
__generated_by__ = "claude-code/claude-opus-5-5"
__generated_at__ = "2026-11-19"

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
from generic_scrapy.poland.common import HTML_PAGE_ERROR, host, parse_datetime, write_bytes
from generic_scrapy.poland.eb2b import EB2B_LIMIT_ERROR, EB2B_LIMIT_RE, EB2B_SLOT, EB2BMixin
from generic_scrapy.poland.ezamowienia import DELETED_AUTOMATICALLY, EZamowieniaMixin
from generic_scrapy.poland.logintrade import LogintradeMixin
from generic_scrapy.poland.marketplanet import MARKETPLANET_HOSTS, MarketplanetMixin
from generic_scrapy.poland.smartpzp import SMARTPZP_SLOT, SmartPZPMixin
from generic_scrapy.poland.ted import TEDMixin, ted_publication_number

HTML_RE = re.compile(rb"\s*(?:<!--.*?-->\s*)*<(?:!doctype html|html|head)\b", re.IGNORECASE | re.DOTALL)
JSON_ERROR_RE = re.compile(rb'\s*\{\s*"success"\s*:\s*false\b')
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


class Poland(TEDMixin, EZamowieniaMixin, MarketplanetMixin, LogintradeMixin, EB2BMixin, SmartPZPMixin, BaseSpider):
    """
    Per-procedure file collector for the Polish public-procurement portal (ezamowienia.gov.pl).

    The e-Zamówienia and TED code and the platform adapters are mixins in generic_scrapy/poland/.

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
