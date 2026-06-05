import json
import re
from pathlib import Path
from urllib.parse import quote

import pymupdf
import scrapy

from generic_scrapy.base_spiders.export_file_spider import ExportFileSpider

# Notice numbers in both KIO rulings and UZP findings are cited on the first few pages — KIO in
# the opening "Sygn. akt" / "Uzasadnienie" block, UZP on the cover sheet of the Informacja PDF.
# We only extract the first PDF_MAX_PAGES pages so the spider stays fast (~20 ms per PDF with
# PyMuPDF) and the regex never has to scan tens of pages of body text.
PDF_MAX_PAGES = 5

# BZP notice numbers in PDFs are usually written without the version suffix ("2023/BZP 00529765"),
# but Board/Search accepts both with and without it, so we capture the unsuffixed form and let the
# server resolve to the latest version. TED notices ("2020/S 013-025754") are captured for the
# record but are NOT resolved here — Board/Search only indexes BZP, so above-EU-threshold
# procurements that publish to TED only will surface with notice_number=None and ted_numbers set.
BZP_NUMBER_RE = re.compile(r"\b\d{4}/BZP\s+\d{6,}", re.IGNORECASE)
TED_NUMBER_RE = re.compile(r"\b\d{4}/S\s+\d+[-\u2013]\d+", re.IGNORECASE)


class PolandCpv(ExportFileSpider):
    """
    Join KIO rulings and UZP findings to their procurement CPV codes.

    For each record produced by ``kio_orzeczenia`` and ``uzp_kontrole``, downloads the associated
    PDF, extracts text with pdfminer.six, regexes out BZP / TED notice numbers, and queries
    ``mo-board/api/v1/Board/Search?NoticeNumber=…`` to pull the ``cpvCode`` field.

    Output: ``data/poland_cpv/<crawl_directory>/joined.json`` — one JSONL row per
    (source, record_id, notice_number) tuple, including rows where extraction yielded nothing
    (notice_number=None) so the miss rate is visible downstream.

    Caveats:

    - Some KIO PDFs are scanned images; pdfminer returns empty / garbled text for those.
    - UZP findings are partly anonymised and the notice number may be redacted in the published
      PDF; expect a non-trivial miss rate on UZP.
    - TED-only notices are captured under ``ted_numbers`` but not resolved to CPV here — that
      would require the EU TED API.
    """

    name = "poland_cpv"

    BOARD_SEARCH_URL = "https://ezamowienia.gov.pl/mo-board/api/v1/Board/Search?NoticeNumber={n}&PageSize=5"

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
        self.stats = {"pdf_ok": 0, "pdf_empty": 0, "pdf_error": 0, "board_hit": 0, "board_miss": 0}

    async def start(self):
        files_store = Path(self.settings["FILES_STORE"])

        kio_dir = self._resolve_crawl(files_store, "kio_orzeczenia", self.kio_crawl)
        if kio_dir:
            for row in _read_jsonl(kio_dir / "rulings.json"):
                yield self._pdf_request(row["pdf_url"], "KIO", row["record_id"], row.get("case_number"))
                self._enqueued += 1
                if self.sample and self._enqueued >= self.sample:
                    return

        uzp_dir = self._resolve_crawl(files_store, "uzp_kontrole", self.uzp_crawl)
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
            yield self._row(source, record_id, label, pdf_url, None, [], [], pdf_status="error")
            return

        if not text.strip():
            self.stats["pdf_empty"] += 1
            yield self._row(source, record_id, label, pdf_url, None, [], [], pdf_status="empty")
            return

        self.stats["pdf_ok"] += 1
        # PDF text often contains double-spaces from layout extraction; collapse to single
        # spaces so Board/Search lookups (which are space-sensitive) succeed.
        bzp_numbers = sorted({re.sub(r"\s+", " ", n).strip() for n in BZP_NUMBER_RE.findall(text)})
        ted_numbers = sorted({re.sub(r"\s+", " ", n).strip() for n in TED_NUMBER_RE.findall(text)})

        if not bzp_numbers:
            yield self._row(source, record_id, label, pdf_url, None, ted_numbers, [], pdf_status="no_notice")
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
                source, record_id, label, pdf_url, notice_number, ted_numbers, [], pdf_status="ok", board_hit=False
            )
            return

        self.stats["board_hit"] += 1
        cpv = []
        for record in notices:
            cpv.extend(_parse_cpv(record.get("cpvCode")))
        yield self._row(
            source, record_id, label, pdf_url, notice_number, ted_numbers, cpv, pdf_status="ok", board_hit=True
        )

    def errback_pdf(self, failure):
        request = failure.request
        self.logger.warning("pdf request failed: %s (%s)", request.url, failure.value)
        self.stats["pdf_error"] += 1
        cb = request.cb_kwargs
        yield self._row(cb["source"], cb["record_id"], cb["label"], cb["pdf_url"], None, [], [], pdf_status="error")

    @staticmethod
    def _row(source, record_id, label, pdf_url, notice_number, ted_numbers, cpv_codes, pdf_status, board_hit=None):
        return {
            "source": source,
            "record_id": record_id,
            "label": label,
            "pdf_url": pdf_url,
            "pdf_status": pdf_status,
            "notice_number": notice_number,
            "ted_numbers": ted_numbers,
            "cpv_codes": cpv_codes,
            "board_hit": board_hit,
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


CPV_RE = re.compile(r"(\d{8}-\d)\s*(?:\(([^)]*)\))?")


def _parse_cpv(cpv_string):
    """
    Parse a Board/Search ``cpvCode`` string into a list of ``{code, label}`` dicts.

    Input is a comma-separated string such as ``"48000000-8 (Pakiety oprogramowania), …"``.
    """
    if not cpv_string:
        return []
    return [{"code": code, "label": (label or "").strip() or None} for code, label in CPV_RE.findall(cpv_string)]
