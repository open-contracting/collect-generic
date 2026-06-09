import json
import re
from pathlib import Path
from urllib.parse import quote

import pymupdf
import scrapy

from generic_scrapy.base_spiders.export_file_spider import ExportFileSpider

# Notice numbers in both KIO rulings and UZP findings are cited on the first few pages — KIO in
# the opening "Sygn. akt" / "Uzasadnienie" block, UZP on the cover sheet of the Informacja PDF.
# Extracting more pages catches notice citations that UZP "Informacja o wyniku kontroli"
# documents bury past the cover sheet. Sampling 30 no-notice UZP PDFs showed ~10% had the
# notice on pages 6-20 and 0% needed pages beyond that. PyMuPDF stays under ~80 ms/PDF at this
# cap.
PDF_MAX_PAGES = 20

# BZP notice numbers in PDFs are usually written without the version suffix ("2023/BZP 00529765"),
# but Board/Search accepts both with and without it, so we capture the unsuffixed form and let the
# server resolve to the latest version. TED notices ("2020/S 013-025754") are captured for the
# record but are NOT resolved here — Board/Search only indexes BZP, so above-EU-threshold
# procurements that publish to TED only will surface with notice_number=None and ted_numbers set.
BZP_NUMBER_RE = re.compile(r"\b\d{4}/BZP\s+\d{6,}", re.IGNORECASE)
TED_NUMBER_RE = re.compile(r"\b\d{4}/S\s+\d+[-\u2013]\d+", re.IGNORECASE)
# Maps the human-readable form "2020/S 013-025754" to TED API publication-number "025754-2020".
TED_PUBLICATION_PARTS_RE = re.compile(r"(\d{4})/S\s+\d+[-\u2013](\d+)", re.IGNORECASE)


class PolandCpv(ExportFileSpider):
    """
    Join KIO rulings and UZP findings to their procurement CPV codes.

    For each record produced by ``poland_kio_orzeczenia`` and ``poland_uzp_kontrole``, downloads
    the associated PDF, extracts text with PyMuPDF, regexes out BZP / TED notice numbers, and
    looks each up against two sources:

    - BZP numbers via ``mo-board/api/v1/Board/Search?NoticeNumber=…`` (Polish procurements).
    - TED numbers via ``POST api.ted.europa.eu/v3/notices/search`` (above-EU-threshold
      procurements). TED returns CPV codes as 8-digit base codes (no checksum); Board/Search
      returns ``XXXXXXXX-X`` with a Polish label. We normalise to the 8-digit base in
      ``cpv_codes`` for uniformity.

    Output: ``data/poland_cpv/<crawl_directory>/joined.json`` — one JSONL row per
    (source, record_id, notice). Rows include ``notice_kind`` ("BZP" or "TED" or null) so you
    can tell which lookup source produced the CPVs.

    Caveats:

    - Some KIO/UZP PDFs are scanned images; PyMuPDF returns empty text. Surfaced as
      ``pdf_status=empty``. OCR is out of scope.
    - UZP findings are partly anonymised and the notice number may be redacted in the published
      "Informacja o wyniku kontroli" PDF.
    - TED's current Search API does not index every pre-migration (pre-~2019) notice; expect
      some misses on older TED references.
    """

    name = "poland_cpv"

    BOARD_SEARCH_URL = "https://ezamowienia.gov.pl/mo-board/api/v1/Board/Search?NoticeNumber={n}&PageSize=5"
    TED_SEARCH_URL = "https://api.ted.europa.eu/v3/notices/search"

    # ExportFileSpider
    export_outputs = {
        "main": {
            "name": "joined",
            "formats": ["json"],
            "item_filter": None,
        },
    }

    custom_settings = {
        # Cache PDFs and Board/Search responses so re-runs after regex tweaks don't re-fetch.
        "HTTPCACHE_ENABLED": True,
        "HTTPCACHE_DIR": "data/poland_cpv/_httpcache",
        "HTTPCACHE_EXPIRATION_SECS": 0,
        "HTTPCACHE_IGNORE_HTTP_CODES": [404, 500, 502, 503],
        # PDFs can be a few MB; bump default 16 MiB to 64 MiB.
        "DOWNLOAD_MAXSIZE": 64 * 1024 * 1024,
    }

    def __init__(self, *args, kio_crawl=None, uzp_crawl=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.kio_crawl = kio_crawl
        self.uzp_crawl = uzp_crawl
        self._enqueued = 0
        self.stats = {
            "pdf_ok": 0,
            "pdf_empty": 0,
            "pdf_error": 0,
            "board_hit": 0,
            "board_miss": 0,
            "ted_hit": 0,
            "ted_miss": 0,
        }

    async def start(self):
        files_store = Path(self.settings["FILES_STORE"])

        kio_dir = self._resolve_crawl(files_store, "poland_kio_orzeczenia", self.kio_crawl)
        if kio_dir:
            for row in _read_jsonl(kio_dir / "rulings.json"):
                yield self._pdf_request(row["pdf_url"], "KIO", row["record_id"], row.get("case_number"))
                self._enqueued += 1
                if self.sample and self._enqueued >= self.sample:
                    return

        uzp_dir = self._resolve_crawl(files_store, "poland_uzp_kontrole", self.uzp_crawl)
        if uzp_dir:
            for idx, row in enumerate(_read_jsonl(uzp_dir / "findings.json")):
                anchor = row.get("anchor") or f"row-{idx}"
                for attachment in row.get("attachments") or []:
                    url = attachment.get("url")
                    if not url:
                        continue
                    yield self._pdf_request(url, "UZP", anchor, row.get("title"))
                self._enqueued += 1
                if self.sample and self._enqueued >= self.sample:
                    return

    def _pdf_request(self, url, source, record_id, label):
        # dont_filter so that multiple UZP findings sharing the same attachment URL each get
        # their own callback (and so their own joined.json row). HTTPCACHE still serves the PDF
        # without an extra download.
        return scrapy.Request(
            url,
            callback=self.parse_pdf,
            errback=self.errback_pdf,
            cb_kwargs={"source": source, "record_id": record_id, "label": label, "pdf_url": url},
            dont_filter=True,
        )

    def parse_pdf(self, response, source, record_id, label, pdf_url):
        # PyMuPDF extracts the first PDF_MAX_PAGES pages in ~20 ms per file, so this stays
        # synchronous — Scrapy tolerates a sub-second block of the reactor per response and
        # the network is the actual bottleneck against this server (~5 s/PDF).
        try:
            text = _extract_first_pages(response.body) or ""
        except (pymupdf.FileDataError, RuntimeError, ValueError) as exc:
            self.logger.warning("pdf parse failed: %s (%s)", pdf_url, exc)
            self.stats["pdf_error"] += 1
            yield self._row(source, record_id, label, pdf_url, None, None, [], [], pdf_status="error")
            return

        if not text.strip():
            self.stats["pdf_empty"] += 1
            yield self._row(source, record_id, label, pdf_url, None, None, [], [], pdf_status="empty")
            return

        self.stats["pdf_ok"] += 1
        # PDF text often contains double-spaces from layout extraction; collapse to single
        # spaces so Board/Search lookups (which are space-sensitive) succeed.
        bzp_numbers = sorted({re.sub(r"\s+", " ", n).strip() for n in BZP_NUMBER_RE.findall(text)})
        ted_numbers = sorted({re.sub(r"\s+", " ", n).strip() for n in TED_NUMBER_RE.findall(text)})

        # Yield TED lookups too — many UZP findings (and a few KIO rulings) cite above-EU
        # procurements that only publish to TED.
        for ted in ted_numbers:
            pub = _ted_publication_number(ted)
            if pub is None:
                continue
            yield scrapy.Request(
                self.TED_SEARCH_URL,
                method="POST",
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                body=json.dumps(
                    {
                        "query": f'publication-number="{pub}"',
                        "fields": ["publication-number", "classification-cpv"],
                    }
                ),
                callback=self.parse_ted_search,
                cb_kwargs={
                    "source": source,
                    "record_id": record_id,
                    "label": label,
                    "pdf_url": pdf_url,
                    "ted_raw": ted,
                    "ted_publication": pub,
                    "ted_numbers": ted_numbers,
                },
                dont_filter=True,
            )

        if not bzp_numbers:
            if not ted_numbers:
                yield self._row(
                    source,
                    record_id,
                    label,
                    pdf_url,
                    None,
                    None,
                    ted_numbers,
                    [],
                    pdf_status="no_notice",
                )
            return

        for notice in bzp_numbers:
            yield scrapy.Request(
                self.BOARD_SEARCH_URL.format(n=quote(notice, safe="")),
                callback=self.parse_board_search,
                cb_kwargs={
                    "source": source,
                    "record_id": record_id,
                    "label": label,
                    "pdf_url": pdf_url,
                    "notice_number": notice,
                    "ted_numbers": ted_numbers,
                },
            )

    def parse_board_search(self, response, source, record_id, label, pdf_url, notice_number, ted_numbers):
        notices = response.json()
        if not notices:
            self.stats["board_miss"] += 1
            yield self._row(
                source,
                record_id,
                label,
                pdf_url,
                notice_number,
                "BZP",
                ted_numbers,
                [],
                pdf_status="ok",
                lookup_hit=False,
            )
            return

        self.stats["board_hit"] += 1
        cpv = []
        for record in notices:
            cpv.extend(_parse_cpv(record.get("cpvCode")))
        yield self._row(
            source,
            record_id,
            label,
            pdf_url,
            notice_number,
            "BZP",
            ted_numbers,
            cpv,
            pdf_status="ok",
            lookup_hit=True,
        )

    def parse_ted_search(self, response, source, record_id, label, pdf_url, ted_raw, ted_numbers, **_):
        payload = response.json()
        notices = payload.get("notices") or []
        if not notices:
            self.stats["ted_miss"] += 1
            yield self._row(
                source,
                record_id,
                label,
                pdf_url,
                ted_raw,
                "TED",
                ted_numbers,
                [],
                pdf_status="ok",
                lookup_hit=False,
            )
            return

        self.stats["ted_hit"] += 1
        cpv = [
            {"code": _normalize_cpv(code), "label": None}
            for notice in notices
            for code in notice.get("classification-cpv") or []
        ]
        yield self._row(
            source,
            record_id,
            label,
            pdf_url,
            ted_raw,
            "TED",
            ted_numbers,
            cpv,
            pdf_status="ok",
            lookup_hit=True,
        )

    def errback_pdf(self, failure):
        request = failure.request
        self.logger.warning("pdf request failed: %s (%s)", request.url, failure.value)
        self.stats["pdf_error"] += 1
        cb = request.cb_kwargs
        yield self._row(
            cb["source"],
            cb["record_id"],
            cb["label"],
            cb["pdf_url"],
            None,
            None,
            [],
            [],
            pdf_status="error",
        )

    @staticmethod
    def _row(
        source,
        record_id,
        label,
        pdf_url,
        notice_number,
        notice_kind,
        ted_numbers,
        cpv_codes,
        pdf_status,
        lookup_hit=None,
    ):
        return {
            "source": source,
            "record_id": record_id,
            "label": label,
            "pdf_url": pdf_url,
            "pdf_status": pdf_status,
            "notice_kind": notice_kind,
            "notice_number": notice_number,
            "ted_numbers": ted_numbers,
            "cpv_codes": cpv_codes,
            "lookup_hit": lookup_hit,
        }

    @staticmethod
    def _resolve_crawl(files_store, spider_name, override):
        spider_dir = files_store / spider_name
        if override:
            return spider_dir / override
        if not spider_dir.is_dir():
            return None
        crawls = sorted(p for p in spider_dir.iterdir() if p.is_dir())
        return crawls[-1] if crawls else None

    def closed(self, _reason):
        self.logger.info("PDF stats: %s", self.stats)


def _read_jsonl(path):
    with path.open() as f:
        for raw_line in f:
            line = raw_line.strip()
            if line:
                yield json.loads(line)


def _extract_first_pages(body):
    """Extract the first ``PDF_MAX_PAGES`` pages of ``body`` using PyMuPDF."""
    with pymupdf.open(stream=body, filetype="pdf") as doc:
        return "\n".join(doc[i].get_text() for i in range(min(PDF_MAX_PAGES, doc.page_count)))


CPV_RE = re.compile(r"(\d{8})(?:-\d)?\s*(?:\(([^)]*)\))?")


def _parse_cpv(cpv_string):
    """
    Parse a Board/Search ``cpvCode`` string into a list of ``{code, label}`` dicts.

    Input is a comma-separated string such as ``"48000000-8 (Pakiety oprogramowania), …"``.
    The trailing check-digit is stripped so codes are comparable with TED responses, which
    return the 8-digit base.
    """
    if not cpv_string:
        return []
    return [{"code": code, "label": (label or "").strip() or None} for code, label in CPV_RE.findall(cpv_string)]


def _normalize_cpv(code):
    """Strip a check-digit suffix if present so all CPV codes are 8-digit strings."""
    if not code:
        return code
    base = code.split("-", 1)[0]
    return base.strip()


def _ted_publication_number(ted_raw):
    """Convert a human-readable TED reference like ``2020/S 013-025754`` to ``025754-2020``."""
    match = TED_PUBLICATION_PARTS_RE.search(ted_raw or "")
    if not match:
        return None
    return f"{match.group(2)}-{match.group(1)}"
