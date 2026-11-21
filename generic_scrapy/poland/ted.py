__review_status__ = "unreviewed"
__generated_by__ = "claude-code/claude-opus-5-5"
__generated_at__ = "2026-11-21"

import html
import io
import json
import re
import tarfile
from http import HTTPStatus
from pathlib import Path

from scrapy.http import JsonRequest

from generic_scrapy.poland.common import (
    HTML_PAGE_ERROR,
    TENDER_ID_RE,
    compact,
    host,
    normalize_url,
    parse_datetime,
    reference_numbers,
    write_bytes,
)

TED_OJS_RE = re.compile(r"(\d{4})/S \d{3}-(\d{6})$")
TED_EFORMS_RE = re.compile(r"\d+-\d{4}$")
URL_DOCUMENT_RE = re.compile(r"<URL_DOCUMENT>\s*([^<\s]+)")
REFERENCE_NUMBER_RE = re.compile(r"<REFERENCE_NUMBER>\s*([^<]+?)\s*<")
# Some buyers copy another procedure's REFERENCE_NUMBER, but give the right one in the title.
TITLE_REFERENCE_RE = re.compile(r"<TITLE>\s*<P>[^<]*?\bnumer postępowania:?\s*([^\s<,;]+)", re.IGNORECASE)
TITLE_RE = re.compile(r"<TITLE>\s*<P>([^<]+)")
# Some buyers append the lot to the procedure's reference in a lot's identifier, e.g. "ZP.371.48.2024.KCCzęść 1".
LOT_SUFFIX_RE = re.compile(r"\s*(?:część|cz\.)\s*\w+$", re.IGNORECASE)
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
    "deadline-receipt-request",
    "ojs-number",
    "buyer-name",
    "buyer-country",
    "title-proc",
    *TED_URL_FIELDS,
]


class TEDMixin:
    """Resolve notices with the TED Search API and notice XML, for the Poland spider."""

    TED_SEARCH_URL = "https://api.ted.europa.eu/v3/notices/search"
    TED_XML_URL = "https://ted.europa.eu/en/notice/{n}/xml"
    TED_PACKAGE_URL = "https://ted.europa.eu/packages/daily/{year}{issue:05d}"
    TED_NOTICE_URL = "https://ted.europa.eu/en/notice/-/detail/{n}"
    TED_PDF_URL = "https://ted.europa.eu/pl/notice/{n}/pdf"

    async def resolve_ted(self, publication_number):
        notices = await self.ted_search(f"publication-number={publication_number}")
        if isinstance(notices, int):
            return {"error": f"{publication_number}: TED search failed (HTTP {notices})"}
        if not notices:
            return {"error": f"{publication_number}: not found in TED"}
        notice = notices[0]
        buyers = ted_buyers(notice)
        if "POL" not in (notice.get("buyer-country") or ["POL"]):
            return {"error": f"{publication_number}: not a Polish buyer's notice ({'; '.join(buyers)})"}
        if procedure := notice.get("procedure-identifier"):
            procedure_notices = await self.ted_search(f'procedure-identifier="{procedure}"')
            if isinstance(procedure_notices, list) and procedure_notices:
                # Some buyers reuse a procedure identifier across procedures.
                notices = [n for n in procedure_notices if same_procedure(notice, n)] or notices
        elif reference := ojs_reference(notice):
            # Pre-eForms corrigenda have no procedure identifier, but cite the notice's OJ S number.
            corrigenda = await self.ted_search(f'notice-type=corr AND FT~"{reference}"')
            if isinstance(corrigenda, list):
                notices = [notice, *corrigenda]
        # Later notices can correct the reference number.
        latest = max(
            (n for n in notices if n.get("internal-identifier-proc") and n.get("notice-type", "").startswith("cn")),
            key=lambda n: n.get("publication-date") or "",
            default=notice,
        )
        result = {
            "buyers": buyers,
            "ted_notices": notices,
            "original_deadline": self.ted_deadline(notice),
            "deadline": (
                self.ted_procedure_deadline(notice, notices)
                if notice.get("procedure-identifier")
                else self.ted_corrigendum_deadline(notices)
            ),
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
        # Pre-eForms notices have one deadline, for tenders or requests to participate, as "deadline-receipt-request".
        requests = map(parse_datetime, notice.get("deadline-receipt-request") or [])
        return max(filter(None, deadlines), default=None) or max(filter(None, requests), default=None)

    @classmethod
    def ted_procedure_deadline(cls, notice, notices):
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

    @classmethod
    def ted_corrigendum_deadline(cls, notices):
        """Return the bid deadline of the latest of the notice and its corrigenda to set one."""
        latest = max(filter(cls.ted_deadline, notices), key=lambda n: n.get("publication-date") or "", default=None)
        return cls.ted_deadline(latest) if latest else None

    def ted_documents(self, notices):
        documents = []
        contract_notices = 0
        for notice in sorted(notices, key=lambda n: (n.get("publication-date") or "", n["publication-number"])):
            if (notice.get("notice-type") or "").startswith("cn"):
                notice_role = "notice_change" if contract_notices else "notice"
                contract_notices += 1
            elif notice.get("notice-type") == "corr":
                notice_role = "notice_change"
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


def ted_publication_number(notice):
    """Return the TED API publication number (``NNNNNN-YYYY``) for an OJ S or eForms notice number."""
    if match := TED_OJS_RE.match(notice):
        return f"{int(match.group(2))}-{match.group(1)}"
    if TED_EFORMS_RE.match(notice):
        return notice
    return None


def ted_buyers(notice):
    names = (notice.get("buyer-name") or {}).values()
    return list(dict.fromkeys(name for value in names for name in ([value] if isinstance(value, str) else value)))


def ojs_reference(notice):
    """Return a pre-eForms notice's OJ S number (``YYYY/S NNN-NNNNNN``), or None."""
    issue, _, year = (notice.get("ojs-number") or "").partition("/")
    number = notice["publication-number"].partition("-")[0]
    if issue.isdigit() and number.isdigit():
        return f"{year}/S {int(issue):03d}-{int(number):06d}"
    return None


def same_procedure(notice, other):
    """Return whether two TED notices with the same procedure identifier share a reference number or title."""
    reference = notice.get("internal-identifier-proc")
    titles = {compact(title) for title in (notice.get("title-proc") or {}).values()}
    if not reference and not titles:
        return True
    if reference and reference == other.get("internal-identifier-proc"):
        return True
    return bool(titles & {compact(title) for title in (other.get("title-proc") or {}).values()})
