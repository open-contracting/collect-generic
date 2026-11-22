__review_status__ = "unreviewed"
__generated_by__ = "claude-code/claude-opus-5-5"
__generated_at__ = "2026-11-21"

import datetime as dt
import json
import re
from http import HTTPStatus
from pathlib import Path
from urllib.parse import quote, urlencode

import scrapy

from generic_scrapy.poland.common import (
    HTML_PAGE_ERROR,
    HTML_RE,
    UNSAFE_FILENAME_RE,
    compact,
    node_text,
    normalize_space,
    normalize_url,
    parse_datetime,
    role,
    text_layer,
    write_bytes,
    write_json,
)

# Matches an h3 prefix in a BZP announcement like "1.5.1.) Ulica" or
# "1.4) Krajowy Numer Identyfikacyjny: REGON 180792174".
# Group 1: dotted key (e.g. "1.5.1"). Group 2: label. Group 3 (optional): post-colon remainder.
ANNOUNCEMENT_KEY_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)*)\.?\)\s*(.*?)(?::\s*(.*))?$", re.DOTALL)
PLATFORM_URL_RE = re.compile(r"Adres strony internetowej prowadzonego postępowania:?\s*(\S+)")
# Documents in this state have no file ("Brak załącznika dla wskazanego dokumentu").
DELETED_AUTOMATICALLY = "DeletedAutomatically"
ORDER_TYPES = {"Delivery": "supplies", "Services": "services", "Works": "works"}
BOARD_NOTICE_TYPES = ("ContractNotice", "NoticeUpdateNotice")
# Board/Search ignores PageSize above 10, so cap the pages read when listing an organization's notices.
MAX_NOTICE_PAGES = 30
# The most days before its TED notice that an e-Zamówienia tender is searched for.
TENDER_INITIATION_DAYS = 90


class EZamowieniaMixin:
    """Collect tenders and BZP notices from e-Zamówienia's APIs, for the Poland spider."""

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
        result = {
            "buyers": [hit["organizationName"]] if hit.get("organizationName") else [],
            "contract_type": ORDER_TYPES.get(hit.get("orderType")),
            "deadline": parse_datetime(hit.get("submittingOffersDate")),
        }
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
        if match := PLATFORM_URL_RE.search(normalize_space(" ".join(response.xpath("//text()").getall()))):
            return {**result, "urls": [normalize_url(match.group(1))]}
        return {**result, "error": f"{notice}: no documents URL in the notice"}

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

    @staticmethod
    def tender_submission_terms(tender):
        return [term for term in tender.get("terms") or [] if term.get("termType") == "SubmissionOffersDate"]

    def tender_final_deadline(self, tender):
        terms = [parse_datetime(t["term"]) for t in self.tender_submission_terms(tender) if t.get("isValid")]
        return max(filter(None, terms), default=None)

    def tender_first_deadline(self, tender):
        terms = self.tender_submission_terms(tender)
        return terms[0]["term"] if terms else None

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
            if document.get("tenderDocumentState") == DELETED_AUTOMATICALLY:
                entry["error"] = "deleted automatically"
            elif document.get("fileName"):
                response = await self.fetch(
                    self.DOWNLOAD_URL.format(tender_id=tender_id, doc_id=document["objectId"]), cache=True
                )
                path = Path(
                    "attachments", UNSAFE_FILENAME_RE.sub("_", f"{document['objectId']}__{document['fileName']}")
                )
                if response.status != HTTPStatus.OK:
                    entry["error"] = f"HTTP {response.status}"
                elif HTML_RE.match(response.body[:512]) and not path.suffix.lower().startswith(".htm"):
                    entry["error"] = HTML_PAGE_ERROR
                else:
                    write_bytes(directory / path, response.body)
                    if path.suffix.lower() == ".pdf":
                        entry["text_layer"] = text_layer(response.body)
                    entry["path"] = str(path)
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
        end = max(filter(None, [self.tender_final_deadline(tender), start + dt.timedelta(days=90)]))
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

    def board_search_request(self, query, callback):
        return scrapy.Request(
            self.BOARD_SEARCH_URL.format(query=urlencode({"PageSize": 10, "PageNumber": 1, **query})),
            meta={"dont_cache": True},
            callback=callback,
            cb_kwargs={"query": query},
        )

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


def _parse_announcement_html(html):
    """
    Parse a Polish BZP announcement (mo-board GetNoticeHtmlBody) into a structured dict.

    Returns ``{"title": str|None, "sections": [...], "_unparsed": [...]}`` where each section
    is ``{"title": str, "items": [{"key", "label", "value", "extra"}]}``.
    """
    sel = scrapy.Selector(text=html)

    title = node_text(sel.xpath("//h1[1]")) or None
    sections = []
    current = None
    unparsed = []

    for node in sel.xpath("//h2 | //h3 | //p"):
        tag = node.root.tag
        if tag == "h2":
            if current is not None:
                sections.append(current)
            current = {"title": node_text(node), "items": []}
        elif tag == "h3":
            full_text = node_text(node)
            span_text = node_text(node.xpath("span[contains(@class, 'normal')]")) or None
            match = ANNOUNCEMENT_KEY_RE.match(full_text)
            if not match:
                unparsed.append({"section": current["title"] if current else None, "text": full_text})
                continue
            key, label, after_colon = (
                match.group(1),
                normalize_space(match.group(2)),
                normalize_space(match.group(3) or "") or None,
            )
            value = span_text or after_colon
            item = {"key": key, "label": label, "value": value, "extra": None}
            (current["items"] if current is not None else unparsed).append(item)
        elif tag == "p":
            text = node_text(node)
            if not text or not current or not current["items"]:
                continue
            last = current["items"][-1]
            last["extra"] = f"{last['extra']} {text}" if last["extra"] else text

    if current is not None:
        sections.append(current)

    return {"title": title, "sections": sections, "_unparsed": unparsed}
