__review_status__ = "unreviewed"
__generated_by__ = "claude-code/claude-opus-5-5"
__generated_at__ = "2026-11-24"

import re
from http import HTTPStatus
from pathlib import Path
from urllib.parse import urlencode, urlparse

import scrapy

from generic_scrapy.poland.common import (
    UNSAFE_FILENAME_RE,
    alphanumeric,
    node_text,
    parse_datetime,
    parse_size,
    role,
    write_bytes,
)

JOSEPHINE_HOST = "josephine.proebiz.com"
JOSEPHINE_TENDER_RE = re.compile(r"/tender/(\d+)")
JOSEPHINE_PROFILE_RE = re.compile(r"/profile/(\d+)")
JOSEPHINE_DATETIME_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4}) (\d{2}:\d{2})")
JOSEPHINE_REFERENCE_RE = re.compile(r"Karta zamówienia #(\S+)")


def josephine_datetime(text):
    if match := JOSEPHINE_DATETIME_RE.search(text or ""):
        return parse_datetime("{2}-{1}-{0} {3}".format(*match.groups()))
    return None


class JosephineMixin:
    """Collect procedures' files from JOSEPHINE (PROEBIZ), for the Poland spider."""

    async def collect_josephine(self, url, references, directory, other_urls=()):
        """Find the procedure on JOSEPHINE and download its files. Return its URLs, deadline and files."""
        origin = "{0.scheme}://{0.netloc}".format(urlparse(url))
        expected = {alphanumeric(reference) for reference in references}
        # A notice can give several tenders' pages, of which only one has its reference number.
        ids = list(
            dict.fromkeys(
                match.group(1)
                for u in [url, *other_urls]
                if urlparse(u).netloc == urlparse(url).netloc and (match := JOSEPHINE_TENDER_RE.search(u))
            )
        )
        if not ids and (match := JOSEPHINE_PROFILE_RE.search(url)):
            if not references:
                return {"error": "no reference number to find the procedure on the platform"}
            ids = await self.josephine_search(f"{origin}/pl/profile/{match.group(1)}", references)
        if not ids:
            return {"error": f"no procedure {' or '.join(references)} on the platform"}

        result = {"urls": [], "documents": [], "deadline": None}
        files = []
        pages = []
        for tender_id in ids:
            response = await self.fetch(f"{origin}/pl/tender/{tender_id}/summary")
            if response.status != HTTPStatus.OK:
                result["error"] = f"procedure page unavailable (HTTP {response.status})"
                continue
            reference = JOSEPHINE_REFERENCE_RE.search(node_text(response.xpath("//h1")))
            pages.append((tender_id, response, reference and alphanumeric(reference.group(1))))
        if len(pages) > 1 and expected:
            pages = [page for page in pages if page[2] in expected] or pages

        for tender_id, response, _ in pages:
            subdirectory = f"josephine-{tender_id}"
            write_bytes(directory / subdirectory / "procedure.html", response.body)
            result["urls"].append(response.url)
            # Some buyers' pages capitalize the label.
            deadline = next(
                (
                    dt.xpath("following-sibling::dd[1]")
                    for dt in response.xpath("//dt")
                    if node_text(dt).lower() == "termin składania ofert"
                ),
                None,
            )
            if deadline and (deadline := josephine_datetime(node_text(deadline))):
                result["deadline"] = max(filter(None, [result["deadline"], deadline]))
            hrefs = set()
            for tr in response.xpath("//tr[td//a[contains(@href, '/summary/download/')]]"):
                href = tr.xpath(".//a[contains(@href, '/summary/download/')]/@href").get()
                if href in hrefs:
                    continue
                hrefs.add(href)
                name = tr.xpath("td[1]/span[not(contains(@class, 'file-icon'))]/@title").get() or node_text(
                    tr.xpath("td[1]")
                )
                kind = node_text(tr.xpath("td[2]"))
                published = josephine_datetime(node_text(tr.xpath("td[4]")))
                entry = {
                    "path": None,
                    "url": response.urljoin(href),
                    "title": f"{kind} / {name}" if kind else name,
                    "file_name": name,
                    "size": parse_size(node_text(tr.xpath("td[3]"))),
                    "published_at": published.isoformat() if published else None,
                    "role": role(kind, name),
                    "superseded": False,
                    "state": None,
                }
                file_id = href.rsplit("/", 1)[-1]
                path = Path(subdirectory, "attachments", UNSAFE_FILENAME_RE.sub("_", f"{file_id}__{name}"))
                files.append((entry, path, scrapy.Request(entry["url"], meta={"dont_cache": True}, dont_filter=True)))
                result["documents"].append(entry)
        await self.download_files(directory, files)
        return result

    async def josephine_search(self, profile_url, references):
        """Return the IDs of the buyer's tenders whose number is one of ``references``."""
        expected = {alphanumeric(reference) for reference in references}
        ids = []
        for reference in references:
            response = await self.fetch(f"{profile_url}?{urlencode({'filter[search]': reference})}")
            if response.status != HTTPStatus.OK:
                self.logger.warning("JOSEPHINE search failed (HTTP %s): %s", response.status, profile_url)
                continue
            ids.extend(
                JOSEPHINE_TENDER_RE.search(tr.xpath(".//a[contains(@href, '/summary')]/@href").get())[1]
                for tr in response.xpath("//tr[td//a[contains(@href, '/summary')]]")
                if alphanumeric(node_text(tr.xpath("td[2]//strong"))) in expected
            )
        return list(dict.fromkeys(ids))
