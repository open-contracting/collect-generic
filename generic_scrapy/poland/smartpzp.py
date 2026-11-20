__review_status__ = "unreviewed"
__generated_by__ = "claude-code/claude-opus-5-5"
__generated_at__ = "2026-11-19"

import io
import re
import unicodedata
import zipfile
from http import HTTPStatus
from pathlib import Path
from urllib.parse import urlparse

import scrapy

from generic_scrapy.poland.common import UNSAFE_FILENAME_RE, _text, parse_datetime, role, words_in_order, write_bytes

SMARTPZP_SLOT = "portal.smartpzp.pl"
SMARTPZP_PROCEDURE_RE = re.compile(r"/public/postepowanie\?postepowanie=(\d+)")
SMARTPZP_DATETIME_RE = re.compile(r"(\d{2})-(\d{2})-(\d{4}) (\d{2}:\d{2})")
SMARTPZP_FORM = "postepowanieTabs:listaDokumentowForm"
SMARTPZP_TABLE = f"{SMARTPZP_FORM}:listaDokumentowTabela"
SMARTPZP_ROWS = 10
# The most documents downloaded in one archive.
SMARTPZP_BATCH = 20


class SmartPZPMixin:
    """Collect procedures' files from SmartPZP, for the Poland spider."""

    async def collect_smartpzp(self, url, references, directory):
        """Find the procedure on SmartPZP and download its files. Return its URLs, deadline and files."""
        # The pages are JSF views, whose state is kept in the session, so each control has its own cookie jar.
        cookiejar = f"smartpzp-{directory.name}"
        if SMARTPZP_PROCEDURE_RE.search(url):
            procedure_urls = [url]
        else:
            if not references:
                return {"error": "no reference number to find the procedure on the platform"}
            parsed = urlparse(url)
            buyer = f"{parsed.scheme}://{parsed.netloc}/{parsed.path.strip('/').split('/')[0]}/"
            procedure_urls = await self.smartpzp_search(buyer, references, cookiejar)
            if not procedure_urls:
                return {"error": f"no procedure {' or '.join(references)} on the platform"}

        result = {"urls": [], "documents": [], "deadline": None}
        for procedure_url in procedure_urls:
            response = await self.fetch(self.smartpzp_request(procedure_url, cookiejar))
            match = re.search(r'listaDokumentowTabela",\{.*?rowCount:(\d+)', response.text if response.status else "")
            if response.status != HTTPStatus.OK or not match:
                result["error"] = f"procedure page unavailable (HTTP {response.status})"
                continue
            subdirectory = f"smartpzp-{SMARTPZP_PROCEDURE_RE.search(procedure_url).group(1)}"
            write_bytes(directory / subdirectory / "procedure.html", response.body)
            result["urls"].append(procedure_url)
            deadline = response.xpath(
                "//span[normalize-space() = 'Termin składania ofert']"
                "/following::span[@class = 'pzp-outputtext-content'][1]"
            )
            if match_deadline := SMARTPZP_DATETIME_RE.match(_text(deadline)):
                deadline = parse_datetime("{2}-{1}-{0} {3}".format(*match_deadline.groups()))
                result["deadline"] = max(filter(None, [result["deadline"], deadline]))

            view_state = response.xpath("//input[@name = 'javax.faces.ViewState']/@value").get()
            rows = response.xpath(f"//tbody[@id = '{SMARTPZP_TABLE}_data']/tr[@data-rk]")
            for first in range(SMARTPZP_ROWS, int(match.group(1)), SMARTPZP_ROWS):
                page = await self.fetch(
                    self.smartpzp_request(
                        procedure_url,
                        cookiejar,
                        {
                            "javax.faces.partial.ajax": "true",
                            "javax.faces.source": SMARTPZP_TABLE,
                            "javax.faces.partial.execute": SMARTPZP_TABLE,
                            "javax.faces.partial.render": SMARTPZP_TABLE,
                            SMARTPZP_TABLE: SMARTPZP_TABLE,
                            f"{SMARTPZP_TABLE}_pagination": "true",
                            f"{SMARTPZP_TABLE}_first": str(first),
                            f"{SMARTPZP_TABLE}_rows": str(SMARTPZP_ROWS),
                            f"{SMARTPZP_TABLE}_encodeFeature": "true",
                            SMARTPZP_FORM: SMARTPZP_FORM,
                            "javax.faces.ViewState": view_state,
                        },
                    )
                )
                rows.extend(smartpzp_update(page, SMARTPZP_TABLE).xpath("//tr[@data-rk]"))

            entries = {}
            for tr in rows:
                name = _text(tr.xpath("td[2]"))
                published = SMARTPZP_DATETIME_RE.match(_text(tr.xpath("td[3]")))
                entries[tr.attrib["data-rk"]] = {
                    "path": None,
                    "url": procedure_url,
                    "title": name,
                    "file_name": name,
                    "size": None,
                    "published_at": (
                        parse_datetime("{2}-{1}-{0} {3}".format(*published.groups())).isoformat()
                        if published
                        else None
                    ),
                    "role": role(name),
                    "superseded": False,
                    "state": None,
                }
            result["documents"].extend(entries.values())
            await self.smartpzp_download(procedure_url, cookiejar, view_state, entries, directory, subdirectory)
        return result

    async def smartpzp_search(self, buyer, references, cookiejar):
        """Return the URLs of the buyer's procedures, finished or not, whose number is one of ``references``."""
        response = await self.fetch(self.smartpzp_request(buyer, cookiejar))
        view_state = response.xpath("//input[@name = 'javax.faces.ViewState']/@value").get()
        if response.status != HTTPStatus.OK or not view_state:
            return []
        urls = []
        table = "listaPostepowanForm:postepowaniaTabela"
        for reference in references:
            # The search matches a substring, so if the reference misses (e.g. "EZP 74/22" for "EZP nr 74/22"), search
            # its longest part with a digit, and compare the numbers' words.
            parts = sorted((part for part in reference.split() if re.search(r"\d", part)), key=len, reverse=True)
            rows = []
            for query in dict.fromkeys([reference, *parts[:1]]):
                # The "Tylko niezakończone" checkbox is left unticked, to include finished procedures.
                search = await self.fetch(
                    self.smartpzp_request(
                        response.url,
                        cookiejar,
                        {
                            "javax.faces.partial.ajax": "true",
                            "javax.faces.source": "wyszukPostepowanForm:wyszukaj",
                            "javax.faces.partial.execute": "wyszukPostepowanForm",
                            "javax.faces.partial.render": table,
                            "wyszukPostepowanForm:wyszukaj": "wyszukPostepowanForm:wyszukaj",
                            "wyszukPostepowanForm": "wyszukPostepowanForm",
                            "wyszukPostepowanForm:numerPostepowania": query,
                            "javax.faces.ViewState": view_state,
                        },
                    )
                )
                rows = [
                    tr
                    for tr in smartpzp_update(search, table).xpath("//tr[@data-rk]")
                    if words_in_order(reference, _text(tr.xpath("td[1]")))
                ]
                if rows:
                    break
            for tr in rows:
                # The procedure page opens by selecting its row and previewing it, which redirects to its URL.
                form = {
                    "listaPostepowanForm": "listaPostepowanForm",
                    f"{table}_selection": tr.attrib["data-rk"],
                    "javax.faces.ViewState": view_state,
                }
                await self.fetch(
                    self.smartpzp_request(
                        response.url,
                        cookiejar,
                        {
                            **form,
                            "javax.faces.partial.ajax": "true",
                            "javax.faces.source": table,
                            "javax.faces.partial.execute": table,
                            "javax.faces.partial.render": "listaPostepowanForm:buttony",
                            "javax.faces.behavior.event": "rowSelect",
                            "javax.faces.partial.event": "rowSelect",
                            f"{table}_instantSelection": tr.attrib["data-rk"],
                        },
                    )
                )
                preview = await self.fetch(
                    self.smartpzp_request(
                        response.url,
                        cookiejar,
                        {**form, "listaPostepowanForm:podgladPublicId": "listaPostepowanForm:podgladPublicId"},
                    )
                )
                if SMARTPZP_PROCEDURE_RE.search(preview.url):
                    urls.append(preview.url)
        return list(dict.fromkeys(urls))

    async def smartpzp_download(self, procedure_url, cookiejar, view_state, entries, directory, subdirectory):
        """Download the documents in archives of files with distinct names, and set each entry's path or error."""
        batches = []
        for key, entry in entries.items():
            batch = next(
                (
                    b
                    for b in batches
                    if len(b) < SMARTPZP_BATCH and all(entries[k]["file_name"] != entry["file_name"] for k in b)
                ),
                None,
            )
            if batch is None:
                batches.append(batch := [])
            batch.append(key)

        for batch in batches:
            response = await self.smartpzp_selection(procedure_url, cookiejar, view_state, batch)
            members = {}
            if response.status == HTTPStatus.OK and response.body.startswith(b"PK\x03\x04"):
                # Read the archive in memory: it's untrusted, so nothing is extracted to disk.
                with zipfile.ZipFile(io.BytesIO(response.body)) as archive:
                    for info in archive.infolist():
                        if not info.is_dir():
                            members[unicodedata.normalize("NFC", Path(zip_name(info)).name)] = archive.read(info)
            else:
                # Some buyers' archives fail, so download one file at a time. A single file isn't archived.
                for key in batch if len(batch) > 1 else []:
                    response = await self.smartpzp_selection(procedure_url, cookiejar, view_state, [key])
                    if response.status == HTTPStatus.OK and b"<html" not in response.body[:512]:
                        members[unicodedata.normalize("NFC", entries[key]["file_name"])] = response.body
                if len(batch) == 1 and response.status == HTTPStatus.OK and b"<html" not in response.body[:512]:
                    members[unicodedata.normalize("NFC", entries[batch[0]]["file_name"])] = response.body
            for key in batch:
                entry = entries[key]
                if (body := members.get(unicodedata.normalize("NFC", entry["file_name"]))) is None:
                    entry["error"] = (
                        f"HTTP {response.status}" if response.status != HTTPStatus.OK else "not in the download"
                    )
                    continue
                path = Path(subdirectory, "attachments", UNSAFE_FILENAME_RE.sub("_", f"{key}__{entry['file_name']}"))
                write_bytes(directory / path, body)
                entry["path"] = str(Path(directory.name, path))
                entry["size"] = len(body)

    async def smartpzp_selection(self, procedure_url, cookiejar, view_state, keys):
        """Select documents and download them: an archive, or the file if there's one."""
        form = {
            SMARTPZP_FORM: SMARTPZP_FORM,
            f"{SMARTPZP_TABLE}_selection": ",".join(keys),
            "javax.faces.ViewState": view_state,
        }
        # Selecting the rows renders the download button, which the download submits.
        await self.fetch(
            self.smartpzp_request(
                procedure_url,
                cookiejar,
                {
                    **form,
                    "javax.faces.partial.ajax": "true",
                    "javax.faces.source": SMARTPZP_TABLE,
                    "javax.faces.partial.execute": SMARTPZP_TABLE,
                    "javax.faces.partial.render": f"{SMARTPZP_FORM}:przyciskiDokumentacja",
                    "javax.faces.behavior.event": "rowSelectCheckbox",
                    "javax.faces.partial.event": "rowSelectCheckbox",
                    f"{SMARTPZP_TABLE}_instantSelection": keys[-1],
                },
            )
        )
        request = self.smartpzp_request(
            procedure_url, cookiejar, {**form, f"{SMARTPZP_FORM}:downloadBtn": f"{SMARTPZP_FORM}:downloadBtn"}
        )
        request.meta.update({"download_timeout": 1800, "max_retry_times": 1})
        return await self.fetch(request)

    @staticmethod
    def smartpzp_request(url, cookiejar, formdata=None):
        meta = {"dont_cache": True, "download_slot": SMARTPZP_SLOT, "cookiejar": cookiejar}
        if formdata is None:
            return scrapy.Request(url, meta=meta, dont_filter=True)
        headers = {"Faces-Request": "partial/ajax"} if "javax.faces.partial.ajax" in formdata else {}
        return scrapy.FormRequest(url, formdata=formdata, headers=headers, meta=meta, dont_filter=True)


def zip_name(info):
    """Return a ZIP member's name. Without the UTF-8 flag, Python decodes it as CP437, but it can be UTF-8 or CP852."""
    if info.flag_bits & 0x800:
        return info.filename
    raw = info.filename.encode("cp437")
    try:
        return raw.decode()
    except UnicodeDecodeError:
        return raw.decode("cp852")


def smartpzp_update(response, component):
    """Return the HTML that a JSF partial response renders for a component."""
    match = re.search(
        rf'<update id="{re.escape(component)}"><!\[CDATA\[(.*?)\]\]></update>',
        response.text if response.status else "",
        re.DOTALL,
    )
    return scrapy.Selector(text=f"<table><tbody>{match.group(1)}</tbody></table>" if match else "<table></table>")
