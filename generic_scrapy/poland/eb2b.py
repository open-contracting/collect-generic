__review_status__ = "unreviewed"
__generated_by__ = "claude-code/claude-opus-5-5"
__generated_at__ = "2026-11-21"

import json
import re
from http import HTTPStatus
from pathlib import Path
from urllib.parse import urlencode, urlparse

import scrapy

from generic_scrapy.poland.common import UNSAFE_FILENAME_RE, alphanumeric, parse_datetime, role, write_bytes

EB2B_LIMIT_ERROR = "platform error: download limit"
# For example: "Przekroczono dozwolony limit pobierania plików [...] (za 19 min. 30 sek.)", "(za  5 sek.)" or "(za  )"
EB2B_LIMIT_RE = re.compile(r"limit pobierania.*?\(\w+\s*(?:(\d+) min\.)?\s*(?:(\d+) sek\.)?\s*\)", re.DOTALL)
EB2B_SLOT = "eb2b.com.pl"
EB2B_HOSTS = (EB2B_SLOT, "ezamowienia.krus.gov.pl", "ezamowienia.ms.gov.pl", "przetargi.pse.pl")
EB2B_PROCEDURE_RE = re.compile(r"/open-preview-auction\.html/(\d+)")
EB2B_PARAMS_PREFIX = 'App.initController("AuctionPreviewOpen", '


class EB2BMixin:
    """Collect procedures' files from buyers' eB2B platforms, for the Poland spider."""

    async def collect_eb2b(self, url, references, directory):
        """Find the procedure on eB2B and download its files. Return its URLs, deadline and files."""
        origin = "{0.scheme}://{0.netloc}".format(urlparse(url))
        if match := EB2B_PROCEDURE_RE.search(url):
            ids = [match.group(1)]
        else:
            if not references:
                return {"error": "no reference number to find the procedure on the platform"}
            ids = await self.eb2b_search(origin, references)
            if not ids:
                # Archived procedures aren't public, but their result publications are.
                if await self.eb2b_search(origin, references, "result-publication", "auction_id"):
                    return {"error": "procedure archived on the platform, so not public"}
                return {
                    "error": f"no procedure {' or '.join(references)} in the buyer's public list, which shows only "
                    "procedures in progress"
                }

        result = {"urls": [], "documents": [], "deadline": None}
        files = []
        for procedure_id in ids:
            procedure_url = f"{origin}/open-preview-auction.html/{procedure_id}"
            response = await self.fetch(self.eb2b_request(procedure_url))
            start = response.text.find(EB2B_PARAMS_PREFIX) if response.status == HTTPStatus.OK else -1
            if start == -1:
                result["error"] = (
                    "procedure not public on the platform"
                    if response.status == HTTPStatus.OK
                    else f"procedure page unavailable (HTTP {response.status})"
                )
                continue
            auction = json.JSONDecoder().raw_decode(response.text, start + len(EB2B_PARAMS_PREFIX))[0]["auction"]
            subdirectory = f"eb2b-{procedure_id}"
            write_bytes(directory / subdirectory / "procedure.html", response.body)
            result["urls"].append(procedure_url)
            end = auction.get("stage_offers_end_date") or auction.get("offers_attachments_deadline_date") or ""
            if deadline := parse_datetime(end[:16] or None):
                result["deadline"] = max(filter(None, [result["deadline"], deadline]))
            if not auction.get("attachments_public_access"):
                result["error"] = "attachments are only for registered participants"
                continue

            query = urlencode({"id": procedure_id, "deleted": 0, "bidder": 0})
            listing = await self.fetch(self.eb2b_request(f"{origin}/auction/auction/list-attachment?{query}"))
            if listing.status != HTTPStatus.OK:
                result["error"] = f"attachments list unavailable (HTTP {listing.status})"
                continue
            write_bytes(directory / subdirectory / "attachments.json", listing.body)
            for attachment in json.loads(listing.body).get("data") or []:
                name = attachment["name"]
                if attachment.get("extension") and not name.lower().endswith(f".{attachment['extension'].lower()}"):
                    name = f"{name}.{attachment['extension']}"
                published = (attachment.get("publication_date") or attachment.get("created_at") or "")[:16]
                entry = {
                    "path": None,
                    "url": f"{origin}/auction/auction/download-attachment/id/{attachment['id']}",
                    "title": attachment["name"],
                    "file_name": name,
                    "size": attachment.get("size"),
                    "published_at": parse_datetime(published).isoformat() if published else None,
                    "role": role(attachment["name"], name),
                    "superseded": attachment.get("is_current") is False,
                    "state": attachment.get("type_name"),
                }
                path = Path(subdirectory, "attachments", UNSAFE_FILENAME_RE.sub("_", f"{attachment['id']}__{name}"))
                files.append((entry, path, self.eb2b_request(entry["url"])))
                result["documents"].append(entry)
        await self.download_files(directory, files)
        return result

    @staticmethod
    def eb2b_request(url):
        # The English pages of some procedures are an error page. Downloads redirect to signed S3 URLs that expire.
        return scrapy.Request(
            url,
            headers={"Accept-Language": "pl"},
            meta={"dont_cache": True, "download_slot": EB2B_SLOT},
            dont_filter=True,
        )

    async def eb2b_search(self, origin, references, controller="auction", id_field="id"):
        """Return the IDs of the procedures whose number contains one of ``references``, ignoring punctuation."""
        ids = []
        for reference in references:
            # The filter matches substrings. If the full reference misses (e.g. on dashes, or if the notice's
            # reference includes the title), try its parts that look like reference numbers (e.g. DZZK/7/BM/2024),
            # then its dotted numbers (e.g. 3.2410.6.2023 in "O.Sz.D-3.2410.6.2023" for "O/Sz.D-3.2410.6.2023").
            parts = [part for part in reference.split() if re.fullmatch(r"(?=.*\d)(?=.*[./-])\S{6,}", part)]
            numbers = re.findall(r"\d+(?:\.\d+){2,}", reference)
            for value in dict.fromkeys([reference, *parts, *numbers]):
                query = urlencode(
                    {"filter": json.dumps([{"property": "auction_number", "value": value}]), "start": 0, "limit": 100}
                )
                response = await self.fetch(self.eb2b_request(f"{origin}/auction/{controller}/list?{query}"))
                if response.status != HTTPStatus.OK:
                    self.logger.warning("eB2B search failed (HTTP %s): %s", response.status, origin)
                    break
                matches = [
                    str(auction[id_field])
                    for auction in json.loads(response.body).get("data") or []
                    if any(
                        alphanumeric(part) in alphanumeric(auction.get("auction_number"))
                        for part in [reference, *parts]
                    )
                ]
                if matches:
                    ids.extend(matches)
                    break
        return list(dict.fromkeys(ids))
