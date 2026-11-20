__review_status__ = "unreviewed"
__generated_by__ = "claude-code/claude-opus-5-5"
__generated_at__ = "2026-11-19"

import re
from http import HTTPStatus
from pathlib import Path
from urllib.parse import urlparse

import scrapy

from generic_scrapy.poland.common import (
    HTML_PAGE_ERROR,
    UNSAFE_FILENAME_RE,
    _norm,
    _text,
    alphanumeric,
    compact,
    parse_datetime,
    parse_size,
    role,
    write_bytes,
)

# The notice lists' path prefixes, like /pn/lasy/demand/notice/publicpzp/ or /app/demand/notice/public/.
MARKETPLANET_LIST_RE = re.compile(
    r"(/(?:pn/[^/\"'?#]+|app)/demand/notice/public(?:pzp)?/)(?:current|during|expired)/list"
)
MARKETPLANET_DETAILS_RE = re.compile(r"/demand/(?:notice/public/)?\d+/(?:notice/public/)?details")
MARKETPLANET_DATETIME_RE = re.compile(r"\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2})?")
MARKETPLANET_STATUSES = ("current", "during", "expired")
# The most days between a TED notice's publication and the start of the Marketplanet procedure found by its title.
MARKETPLANET_START_DAYS = 14
# The longest title search, and the fewest characters of a title to compare.
MARKETPLANET_QUERY_LENGTH = 60
MARKETPLANET_TITLE_MATCH = 30
# Marketplanet hosts its own platform (ezamawiajacy.pl) and buyers' white-label platforms.
MARKETPLANET_HOSTS = ("ezamawiajacy.pl", "platformazakupowa.plk-sa.pl", "lasy.gov.pl")


class MarketplanetMixin:
    """Collect procedures' files from Marketplanet platforms, for the Poland spider."""

    async def collect_marketplanet(self, url, references, directory, notice=None):
        """Find the procedure on Marketplanet and download its files. Return its URLs, deadline and files."""
        origin = "{0.scheme}://{0.netloc}".format(urlparse(url))
        if MARKETPLANET_DETAILS_RE.search(url):
            details_urls = [url]
        else:
            if not references:
                return {"error": "no reference number to find the procedure on the platform"}
            details_urls = await self.marketplanet_search(origin, references)
            if details_urls == [] and notice:
                details_urls = await self.marketplanet_title_search(origin, notice["title"], notice["published"])
            if details_urls is None:
                return {"error": "no notice lists on the platform's home page"}
            if not details_urls:
                return {"error": f"no procedure {' or '.join(references)} on the platform"}

        result = {"urls": [], "documents": [], "deadline": None}
        files = []
        procedures = []
        for details_url in details_urls:
            response = await self.fetch(details_url)
            if response.status != HTTPStatus.OK or not MARKETPLANET_DETAILS_RE.search(response.url):
                result["error"] = f"procedure page unavailable (HTTP {response.status})"
                continue
            match = re.search(r"/(\d+)/details", response.url)
            subdirectory = f"marketplanet-{match.group(1)}"
            result["urls"].append(response.url)
            deadline = response.xpath(
                "//fieldset[span[contains(., 'składania ofert') or contains(., 'zbierania ofert')]]/section"
            ).xpath("normalize-space()")
            if match := MARKETPLANET_DATETIME_RE.match(deadline.get(default="")):
                result["deadline"] = max(filter(None, [result["deadline"], parse_datetime(match.group(0))]))

            # The procedure's folders (like the results) are subpages, with the same files table.
            pages = [("procedure", response)]
            for href in dict.fromkeys(response.xpath("//a[contains(@href, 'details?folder=')]/@href").getall()):
                folder = re.search(r"folder=(\w+)", href).group(1)
                folder_response = await self.fetch(response.urljoin(href))
                if folder_response.status == HTTPStatus.OK:
                    pages.append((f"procedure-{folder}", folder_response))
            for name, page in pages:
                write_bytes(directory / subdirectory / f"{name}.html", page.body)
            procedure_files = self.marketplanet_files(pages, origin, subdirectory)
            procedures.append((subdirectory, [(name, page.url) for name, page in pages]))
            files.extend(procedure_files)
            result["documents"].extend(entry for entry, _, _ in procedure_files)
        await self.download_files(directory, files)

        # A file's link expires a while after the page view, so re-read the pages for new links, and retry.
        for _ in range(2):
            expired = {path: entry for entry, path, _ in files if entry.get("error") == HTML_PAGE_ERROR}
            if not expired:
                break
            retry = []
            for subdirectory, page_urls in procedures:
                pages = [(name, await self.fetch(page_url)) for name, page_url in page_urls]
                pages = [(name, page) for name, page in pages if page.status == HTTPStatus.OK]
                for _, path, request in self.marketplanet_files(pages, origin, subdirectory):
                    if entry := expired.get(path):
                        entry.pop("error")
                        entry["url"] = request.url
                        retry.append((entry, path, request))
            await self.download_files(directory, retry)
        return result

    def marketplanet_files(self, pages, origin, subdirectory):
        """Return the ``(entry, path, request)`` of each file in the pages' files tables."""
        files = []
        tokens = set()
        for _, page in pages:
            for tr in page.xpath("//tr[.//input[contains(@name, 'selectedFile')]]"):
                token = tr.xpath(".//input[contains(@name, 'selectedFile')]/@value").get()
                if token in tokens:
                    continue
                tokens.add(token)
                # A folder's files are read from its subpage. Skip it after numbering it, to keep the files' paths.
                if tr.xpath(".//a[contains(@href, 'details?folder=')]"):
                    continue
                title = _text(tr.xpath("td[contains(@class, 'text')][1]"))
                published = MARKETPLANET_DATETIME_RE.match(_text(tr.xpath("td[@class='date-time']")))
                entry = {
                    "path": None,
                    "url": f"{origin}/repository/download/{token}",
                    "title": title,
                    "file_name": title,
                    "size": parse_size(_text(tr.xpath("td[contains(@class, 'medium')]"))),
                    "published_at": parse_datetime(published.group(0)).isoformat() if published else None,
                    "role": role(title),
                    "superseded": False,
                    "state": _norm(
                        tr.xpath("ancestor::div[contains(@class, 'mp_box')][1]//h2").xpath("string()").get()
                    ),
                }
                # The path is from the file's position, as its link changes. Downloads can't be cached.
                path = Path(subdirectory, "attachments", UNSAFE_FILENAME_RE.sub("_", f"{len(tokens):03d}__{title}"))
                request = scrapy.Request(entry["url"], method="POST", meta={"dont_cache": True}, dont_filter=True)
                files.append((entry, path, request))
        return files

    async def marketplanet_search(self, origin, references):
        """Return the details URLs of the procedures matching a reference, or None if there are no notice lists."""
        response = await self.fetch(f"{origin}/servlet/HomeServlet")
        prefixes = list(dict.fromkeys(MARKETPLANET_LIST_RE.findall(response.text if response.status else "")))
        if not prefixes:
            return None
        urls = []
        for prefix in prefixes:
            # The PZP lists link to the details page outside the "publicpzp" path.
            details_prefix = prefix.replace("/publicpzp/", "/public/")
            for reference in references:
                for status in MARKETPLANET_STATUSES:
                    search = await self.fetch(
                        scrapy.FormRequest(
                            f"{origin}{prefix}{status}/list",
                            formdata={"demandNumber": reference},
                            meta={"dont_cache": True},
                            dont_filter=True,
                        )
                    )
                    if search.status != HTTPStatus.OK:
                        self.logger.warning("Marketplanet search failed (HTTP %s): %s", search.status, origin)
                        continue
                    urls.extend(
                        f"{origin}{details_prefix}{tr.attrib['id']}/details"
                        for tr in search.css("tr.dataRow")
                        if compact(reference) in compact(_text(tr.xpath("td[1]")))
                    )
        return list(dict.fromkeys(urls))

    async def marketplanet_title_search(self, origin, title, published):
        """Return the details URL of the only procedure with the notice's title that started near its publication."""
        response = await self.fetch(f"{origin}/servlet/HomeServlet")
        prefixes = list(dict.fromkeys(MARKETPLANET_LIST_RE.findall(response.text if response.status else "")))
        # The search matches a substring, so search the title's longest run of words without punctuation.
        words = max(re.split(r"[^\w\s-]+", title), key=len).strip()
        query = words
        if len(words) > MARKETPLANET_QUERY_LENGTH:
            query = words[:MARKETPLANET_QUERY_LENGTH].rsplit(" ", 1)[0]
        expected = alphanumeric(title)
        urls = []
        for prefix in prefixes:
            details_prefix = prefix.replace("/publicpzp/", "/public/")
            for status in MARKETPLANET_STATUSES:
                search = await self.fetch(
                    scrapy.FormRequest(
                        f"{origin}{prefix}{status}/list",
                        formdata={"demandName": query},
                        meta={"dont_cache": True},
                        dont_filter=True,
                    )
                )
                if search.status != HTTPStatus.OK:
                    continue
                for tr in search.css("tr.dataRow"):
                    # Long titles are truncated with "(..)".
                    name = alphanumeric(_text(tr.xpath("td[2]")).removesuffix("(..)"))
                    length = min(len(name), len(expected), 100)
                    # The dates columns, like the procedure's start and its publication.
                    dates = [parse_datetime(_text(td) or None) for td in tr.xpath("td[@data-width = '110']")]
                    if (
                        length >= MARKETPLANET_TITLE_MATCH
                        and name[:length] == expected[:length]
                        and any(d and abs((d - published).days) <= MARKETPLANET_START_DAYS for d in dates)
                    ):
                        urls.append(f"{origin}{details_prefix}{tr.attrib['id']}/details")
        urls = list(dict.fromkeys(urls))
        return urls if len(urls) == 1 else []
