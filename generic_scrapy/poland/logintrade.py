__review_status__ = "unreviewed"
__generated_by__ = "claude-code/claude-opus-5-5"
__generated_at__ = "2026-11-19"

import re
from http import HTTPStatus
from pathlib import Path
from urllib.parse import urlencode, urlparse

import scrapy

from generic_scrapy.poland.common import (
    UNSAFE_FILENAME_RE,
    _text,
    compact,
    host,
    parse_datetime,
    parse_size,
    role,
    write_bytes,
)
from generic_scrapy.poland.marketplanet import MARKETPLANET_DATETIME_RE

LOGINTRADE_PROCEDURE_RE = re.compile(r"/zapytania_email,(\d+),\w+\.html")
LOGINTRADE_DATETIME_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4}) - (\d{2}:\d{2})")


class LogintradeMixin:
    """Collect procedures' files from buyers' Logintrade platforms, for the Poland spider."""

    async def collect_logintrade(self, url, references, directory, other_urls=()):
        """Find the procedure on Logintrade and download its files. Return its URLs, deadline and files."""
        origin = "{0.scheme}://{0.netloc}".format(urlparse(url))
        if LOGINTRADE_PROCEDURE_RE.search(url):
            procedure_urls = [url]
            # A buyer can link another procedure's page as the documents URL, but its own as the submission URL.
            candidates = [u for u in other_urls if LOGINTRADE_PROCEDURE_RE.search(u) and host(u) == host(url)]
            if references and len(dict.fromkeys([url, *candidates])) > 1:
                for candidate in dict.fromkeys([url, *candidates]):
                    response = await self.fetch(candidate)
                    signature = _text(
                        response.xpath(
                            "//h4[normalize-space() = 'Znak sprawy' or normalize-space() = 'Signature of the case']"
                            "/following-sibling::div[1]"
                        )
                    )
                    if any(compact(reference) in compact(signature) for reference in references):
                        procedure_urls = [candidate]
                        break
        else:
            if not references:
                return {"error": "no reference number to find the procedure on the platform"}
            procedure_urls = []
            for reference in references:
                # The statutory (PZP) procedures, in any state.
                query = urlencode({"status": "", "szukaj": reference})
                search = await self.fetch(f"{origin}/rejestracja/ustawowe.html?{query}")
                if search.status == HTTPStatus.UNAUTHORIZED:
                    return {"error": "the buyer's portal requires a login (HTTP 401)"}
                if search.status != HTTPStatus.OK:
                    self.logger.warning("Logintrade search failed (HTTP %s): %s", search.status, origin)
                    continue
                for a in search.xpath("//tr[td]//a[contains(@href, 'zapytania_email')]"):
                    if compact(reference) in compact(_text(a.xpath("ancestor::tr[1]/td[3]"))):
                        procedure_urls.append(search.urljoin(a.attrib["href"]))
            procedure_urls = list(dict.fromkeys(procedure_urls))
            if not procedure_urls:
                return {"error": f"no procedure {' or '.join(references)} on the platform"}

        result = {"urls": [], "documents": [], "deadline": None}
        files = []
        for procedure_url in procedure_urls:
            response = await self.fetch(procedure_url)
            match = LOGINTRADE_PROCEDURE_RE.search(response.url)
            if response.status != HTTPStatus.OK or not match:
                result["error"] = f"procedure page unavailable (HTTP {response.status})"
                continue
            subdirectory = f"logintrade-{match.group(1)}"
            write_bytes(directory / subdirectory / "procedure.html", response.body)
            result["urls"].append(response.url)
            # Scrapy is served the English page.
            deadline = response.xpath(
                "//h4[normalize-space() = 'Termin składania' or normalize-space() = 'Deadline for making offers']"
                "/following-sibling::div[1]"
            )
            if match := MARKETPLANET_DATETIME_RE.match(_text(deadline)):
                result["deadline"] = max(filter(None, [result["deadline"], parse_datetime(match.group(0))]))

            # The page can list an attachment twice (e.g. under the notice and in the document list).
            hrefs = set()
            for div in response.css("div.attachment"):
                href = div.xpath(".//a[contains(@href, 'getAttachmentUnlogged')]/@href").get()
                if not href or href in hrefs:
                    continue
                hrefs.add(href)
                title = _text(div.css(".attachment-name"))
                published = LOGINTRADE_DATETIME_RE.match(_text(div.css(".attachment-date")))
                entry = {
                    "path": None,
                    "url": response.urljoin(href),
                    "title": title,
                    "file_name": title,
                    "size": parse_size(_text(div.css(".attachment-size"))),
                    "published_at": (
                        parse_datetime("{2}-{1}-{0} {3}".format(*published.groups())).isoformat()
                        if published
                        else None
                    ),
                    "role": role(title),
                    "superseded": False,
                    "state": None,
                }
                path = Path(subdirectory, "attachments", UNSAFE_FILENAME_RE.sub("_", f"{len(hrefs):03d}__{title}"))
                files.append((entry, path, scrapy.Request(entry["url"], dont_filter=True)))
                result["documents"].append(entry)
        await self.download_files(directory, files)
        return result
