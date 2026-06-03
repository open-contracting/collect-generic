import json
import re
from pathlib import Path
from urllib.parse import quote

import scrapy

from generic_scrapy.base_spiders.base_spider import BaseSpider
from generic_scrapy.parsers.poland_announcement import parse_announcement

UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


class Poland(BaseSpider):
    """
    Per-procedure file collector for the Polish public-procurement portal (ezamowienia.gov.pl).

    Walks the SearchTenders catalogue (sorted DESC by InitiationDate) and, for each tender, writes:
      - tender.json         — Search/GetTender response
      - documents.json      — Search/GetTenderDocuments response (public download URLs)
      - notice_meta.json    — Board/GetNoticeDetails response (when bzpNumber is set)
      - announcement.html   — raw Board/GetNoticeHtmlBody body
      - announcement.json   — announcement.html parsed into structured sections
      - attachments/<documentId>__<filename> — one file per non-HTML tender attachment

    Output layout: ``<FILES_STORE>/poland/<crawl_directory>/<tenderId>/``. Re-running with the same
    ``crawl_directory`` skips tenders whose ``tender.json`` is already on disk.
    """

    name = "poland"

    SEARCH_URL = (
        "https://ezamowienia.gov.pl/mp-readmodels/api/Search/SearchTenders"
        "?Page={page}&PageSize=100&SortingColumnName=InitiationDate&SortingDirection=DESC"
    )
    TENDER_URL = "https://ezamowienia.gov.pl/mp-readmodels/api/Search/GetTender?id={id}"
    DOCUMENTS_URL = "https://ezamowienia.gov.pl/mp-readmodels/api/Search/GetTenderDocuments?tenderId={id}"
    DOWNLOAD_URL = "https://ezamowienia.gov.pl/mp-readmodels/api/Tender/DownloadDocument/{tender_id}/{doc_id}"
    # An mp-readmodels/api/Tender/GetTenderNoticeDetails endpoint returns the same shape as Board/GetNoticeDetails.
    # Board is used here because its sibling GetNoticeHtmlBody has no mp-readmodels equivalent.
    NOTICE_DETAILS_URL = "https://ezamowienia.gov.pl/mo-board/api/v1/Board/GetNoticeDetails?noticeNumber={n}"
    NOTICE_HTML_URL = "https://ezamowienia.gov.pl/mo-board/api/v1/Board/GetNoticeHtmlBody?noticeNumber={n}"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._yielded = 0

    async def start(self):
        yield scrapy.Request(self.SEARCH_URL.format(page=1), callback=self.parse_search)

    def parse_search(self, response):
        pagination = json.loads(response.headers.get("X-Pagination", b"{}").decode("utf-8") or "{}")
        for summary in response.json():
            tender_id = summary["objectId"]
            if (self._tender_dir(tender_id) / "tender.json").exists():
                continue
            yield scrapy.Request(
                self.TENDER_URL.format(id=tender_id),
                callback=self.parse_tender,
                cb_kwargs={"tender_id": tender_id},
            )
            self._yielded += 1
            if self.sample and self._yielded >= self.sample:
                return
        if pagination.get("HasNext"):
            yield scrapy.Request(
                self.SEARCH_URL.format(page=pagination["CurrentPage"] + 1),
                callback=self.parse_search,
            )

    def parse_tender(self, response, tender_id):
        tender = response.json()
        self._write_json(tender_id, "tender.json", tender)

        for document in tender.get("tenderDocuments") or []:
            attachment = document.get("attachment")
            if not attachment or attachment.get("isDeleted") or attachment.get("mimeType") == "text/html":
                continue
            yield scrapy.Request(
                self.DOWNLOAD_URL.format(tender_id=tender_id, doc_id=document["objectId"]),
                callback=self.parse_attachment,
                cb_kwargs={
                    "tender_id": tender_id,
                    "doc_id": document["objectId"],
                    "filename": attachment["fileName"],
                    "expected_size": attachment.get("fileSize"),
                },
            )

        yield scrapy.Request(
            self.DOCUMENTS_URL.format(id=tender_id),
            callback=self.parse_documents,
            cb_kwargs={"tender_id": tender_id},
        )

        notice_number = tender.get("bzpNumber") or tender.get("noticeNumber")
        if notice_number:
            encoded = quote(notice_number, safe="")
            yield scrapy.Request(
                self.NOTICE_DETAILS_URL.format(n=encoded),
                callback=self.parse_notice_meta,
                cb_kwargs={"tender_id": tender_id, "encoded_notice_number": encoded},
            )

    def parse_documents(self, response, tender_id):
        self._write_json(tender_id, "documents.json", response.json())

    def parse_attachment(self, response, tender_id, doc_id, filename, expected_size):
        path = self._tender_dir(tender_id) / "attachments" / UNSAFE_FILENAME_RE.sub("_", f"{doc_id}__{filename}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(response.body)
        if expected_size and len(response.body) != expected_size:
            self.logger.warning(
                "size mismatch for %s/%s: got %d bytes, expected %d",
                tender_id,
                doc_id,
                len(response.body),
                expected_size,
            )

    def parse_notice_meta(self, response, tender_id, encoded_notice_number):
        self._write_json(tender_id, "notice_meta.json", response.json())
        yield scrapy.Request(
            self.NOTICE_HTML_URL.format(n=encoded_notice_number),
            callback=self.parse_announcement,
            cb_kwargs={"tender_id": tender_id},
        )

    def parse_announcement(self, response, tender_id):
        path = self._tender_dir(tender_id)
        path.mkdir(parents=True, exist_ok=True)
        (path / "announcement.html").write_text(response.text, encoding="utf-8")
        parsed = parse_announcement(response.text)
        parsed["raw_html_path"] = "announcement.html"
        self._write_json(tender_id, "announcement.json", parsed)

    def _tender_dir(self, tender_id):
        return Path(self.settings["FILES_STORE"], self.name, self.crawl_directory, tender_id)

    def _write_json(self, tender_id, filename, data):
        path = self._tender_dir(tender_id)
        path.mkdir(parents=True, exist_ok=True)
        (path / filename).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
