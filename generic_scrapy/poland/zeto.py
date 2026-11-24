__review_status__ = "unreviewed"
__generated_by__ = "claude-code/claude-opus-5-5"
__generated_at__ = "2026-11-24"

import json
import re
from http import HTTPStatus
from pathlib import Path
from urllib.parse import urlparse

import scrapy

from generic_scrapy.poland.common import UNSAFE_FILENAME_RE, alphanumeric, parse_datetime, role, write_bytes

# Buyers' instances of ZETO's Platforma Zamówień Publicznych (PZP).
ZETO_HOSTS = ("zamowienia.olsztyn.eu", "przetargi.lubelskie.straz.gov.pl")
ZETO_OFFER_RE = re.compile(r"#/offer/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")
# The longest title search.
ZETO_QUERY_LENGTH = 60


def zeto_datetime(value):
    """Parse the API's local time, like ``2025-02-19T08:25:27.6798724``."""
    return parse_datetime(value[:16].replace("T", " ")) if value else None


class ZetoMixin:
    """Collect procedures' files from buyers' ZETO PZP platforms, for the Poland spider."""

    async def collect_zeto(self, url, references, directory, notice=None):
        """Find the procedure on ZETO PZP and download its files. Return its URLs, deadline and files."""
        origin = "{0.scheme}://{0.netloc}".format(urlparse(url))
        if match := ZETO_OFFER_RE.search(url):
            ids = [match.group(1)]
        else:
            if not references:
                return {"error": "no reference number to find the procedure on the platform"}
            ids = await self.zeto_search(origin, references, (notice or {}).get("title"))
            if not ids:
                return {"error": f"no procedure {' or '.join(references)} on the platform"}

        result = {"urls": [], "documents": [], "deadline": None}
        files = []
        for procedure_id in ids:
            response = await self.fetch(f"{origin}/api/Offers/{procedure_id}/Offers")
            if response.status != HTTPStatus.OK:
                result["error"] = f"procedure unavailable (HTTP {response.status})"
                continue
            procedure = json.loads(response.body)
            subdirectory = f"zeto-{procedure_id}"
            write_bytes(directory / subdirectory / "procedure.json", response.body)
            result["urls"].append(f"{origin}/#/offer/{procedure_id}")
            if deadline := zeto_datetime(procedure.get("offersDate")):
                result["deadline"] = max(filter(None, [result["deadline"], deadline]))
            for i, attachment in enumerate(procedure.get("attachments") or [], 1):
                name = attachment["name"]
                published = zeto_datetime(attachment.get("addedDate"))
                entry = {
                    "path": None,
                    "url": f"{origin}/api/Offers/{procedure_id}/Attachments/{attachment['attachmentUuid']}",
                    "title": name,
                    "file_name": name,
                    "size": attachment.get("size"),
                    "published_at": published.isoformat() if published else None,
                    "role": role(name),
                    "superseded": False,
                    "state": None,
                }
                path = Path(subdirectory, "attachments", UNSAFE_FILENAME_RE.sub("_", f"{i:03d}__{name}"))
                files.append((entry, path, scrapy.Request(entry["url"], meta={"dont_cache": True}, dont_filter=True)))
                result["documents"].append(entry)
        await self.download_files(directory, files)
        return result

    async def zeto_search(self, origin, references, title):
        """Return the IDs of the procedures whose number is one of ``references``, among those matching the title."""
        if not title:
            return []
        # The search matches procedures' names, not numbers, so search the title's longest run of words.
        words = max(re.split(r"[^\w\s-]+", title), key=len).strip()[:ZETO_QUERY_LENGTH].rsplit(" ", 1)[0]
        request = scrapy.Request(
            f"{origin}/api/Offers",
            method="POST",
            body=json.dumps({"pageNumber": 0, "searchText": words}),
            headers={"Content-Type": "application/json"},
            meta={"dont_cache": True},
            dont_filter=True,
        )
        response = await self.fetch(request)
        if response.status != HTTPStatus.OK:
            self.logger.warning("ZETO PZP search failed (HTTP %s): %s", response.status, origin)
            return []
        expected = {alphanumeric(reference) for reference in references}
        return [
            item["uuid"]
            for item in json.loads(response.body).get("items") or []
            if alphanumeric(item.get("number")) in expected
        ]
