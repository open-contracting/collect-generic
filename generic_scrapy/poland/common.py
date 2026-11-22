__review_status__ = "unreviewed"
__generated_by__ = "claude-code/claude-opus-5-5"
__generated_at__ = "2026-11-22"

import datetime as dt
import html
import json
import re
import struct
import unicodedata
import zlib
import zoneinfo
from pathlib import Path
from urllib.parse import urlparse

import pymupdf

HTML_PAGE_ERROR = "HTML page instead of the file"
HTML_RE = re.compile(rb"\s*(?:<!--.*?-->\s*)*<(?:!doctype html|html|head)\b", re.IGNORECASE | re.DOTALL)
UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
TENDER_ID_RE = re.compile(r"ocds-148610-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# Hosts misspelled in notices' documents URLs.
HOST_TYPOS = {
    "platformazakupowaz.plk-sa.pl": "platformazakupowa.plk-sa.pl",
    "www.platformazakupowa-plk-sa.pl": "platformazakupowa.plk-sa.pl",
}
WARSAW = zoneinfo.ZoneInfo("Europe/Warsaw")
BUYER_STOPWORDS = {"spolka", "ograniczona", "odpowiedzialnoscia", "akcyjna", "the"}

# Some platforms drop or transliterate Polish letters in filenames, so roles are matched without them.
FOLD = str.maketrans("ąćęłńóśźż", "acelnoszz")
# Checked in order against the document's name and filename.
ROLES = [
    ("notice_change", r"zmian\w* ogłosz|ogłoszeni\w* o zmianie|sprostowani"),
    ("swz_change", r"zmian\w* (?:nr \d+ )?(?:tre[sś]ci )?(?:swz|idw|specyfikacji|instrukcji dla wykonawc)|modyfikacj"),
    ("qa", r"wyjaśnie|odpowied\w* na pytani|pytania i odpowiedzi|\bpytani|\bpyt\w* i odp|zapyta\w* wraz z odpowiedzi"),
    # Appeals, calls to join them, buyers' answers and KIO's rulings. SWZ changes made due to an appeal match above.
    # "odwoanie" is "odwołanie" with the ł dropped.
    (
        "appeal",
        r"odwo[lł]?a(?:ni|ń)|o złożeniu odw|\bkio\b|wyrok|postępowani\w* odwoławcz|izb\w* odwoławcz|sprzeciw",
    ),
    ("opz", r"\bs?opz\b|opis\w* przedmiotu zamówienia|\bpfu\b|program\w* funkcjonalno"),
    ("ppu", r"\bppu\b|projektowan\w* postanowie|wz[oó]r umowy|istotne postanowienia|warunki (?:kontraktu|umowy)"),
    ("notice", r"^ogłoszeni|\bojs\d{3}\b"),
    # The SWZ packaged with its annexes, like "Tom I - IDW z załącznikami", or a name that starts with the SWZ and
    # isn't a numbered annex, like "SWZ - PUI Modele z załącznikami 3, 5-13" but not "SWZ_Z632025_Zał 1".
    ("swz", r"(?:\bswz\b|\bidw\b|\btom i\b|instrukcj\w* dla wykonawc\w*)\W*(?:wraz z|z|\+)\s*załącznik"),
    (
        "swz",
        r"^[\W\d]*(?:swz|idw|tom i|specyfikacj\w* warunk|instrukcj\w* dla wykonawc)\b(?!.*\bzał\.?\s*(?:nr\s*)?\d)",
    ),
    ("annex", r"załącznik|\bza[lł]\.?\s*(?:nr|\d)|\bzal\b"),
    # Some buyers (e.g. PKP PLK) call the SWZ the "Instrukcja dla Wykonawców" (IDW), or volume I ("Tom I").
    ("swz", r"\bswz\b|specyfikacj\w* warunk\w* zamówienia|\bidw\b|instrukcj\w* dla wykonawc|\btom i\b"),
]
ROLES = [(name, re.compile(pattern.translate(FOLD))) for name, pattern in ROLES]

# Large files that needn't be downloaded: drawings and other design documentation, and bills of quantities. Everything
# else is key, including technical specifications (STWiORB, opis techniczny) and pricing forms, which can set
# technical parameters.
DRAWING_EXTENSIONS = {".dwg", ".dxf", ".dwf", ".dgn", ".tif", ".tiff", ".ifc", ".rvt", ".skp"}
SKIPPABLE = [
    ("bill_of_quantities", r"przedmiar|kosztorys\w* inwestorsk|ślep\w* kosztorys|\.ath\b"),
    (
        "design_documentation",
        (
            r"rysun|\brys\.|projekt\w* (?:wykonawcz|budowlan|techniczn|architektoniczn)|dokumentacj\w* projektow"
            r"|dok\.? ?proj|\bp[bw]\b|część rysunkow|plan\w* sytuacyjn|inwentaryzacj|geolog|geotechn|drawing|design"
        ),
    ),
]
SKIPPABLE = [(name, re.compile(pattern.translate(FOLD))) for name, pattern in SKIPPABLE]
# Key whatever else the name says: pricing forms (formularz cenowy, RCO), technical specifications and the PFU.
KEY_RE = re.compile(
    r"\brco\b|formularz\w* (?:cenow|asortyment|parametr)|kosztorys\w* ofertow|stwi?orb?|\bs?st\b"
    r"|specyfikacj\w* techniczn|opis\w* techniczn|\bpfu\b|program\w* funkcjonalno".translate(FOLD)
)
# Characters per page below which a PDF is a scan or mostly images, as in oecd-poland's analysis/inventory.py.
SCANNED_CHARS_PER_PAGE = 50
LOW_TEXT_CHARS_PER_PAGE = 300
TEXT_LAYER_PAGES = 20
# A ZIP file's member-list offset when the real one is in a ZIP64 record.
ZIP64_OFFSET = 0xFFFFFFFF


def normalize_space(text):
    return " ".join((text or "").split())


def node_text(selector):
    return normalize_space(" ".join(selector.xpath(".//text()").getall()))


def role(*names):
    """Return the document's role (swz, opz, ppu, annex, swz_change, qa, appeal, notice, notice_change or other)."""
    text = " ".join(normalize_space(name).lower().replace("_", " ") for name in names if name)
    text = unicodedata.normalize("NFC", text).translate(FOLD)
    return next((name for name, pattern in ROLES if pattern.search(text)), "other")


def skippable(*names):
    """Return whether a file is ``design_documentation`` or a ``bill_of_quantities`` by its names, or None."""
    if any(Path(name).suffix.lower() in DRAWING_EXTENSIONS for name in names if name):
        return "design_documentation"
    text = " ".join(normalize_space(name).lower().replace("_", " ") for name in names if name)
    text = unicodedata.normalize("NFC", text).translate(FOLD)
    if KEY_RE.search(text):
        return None
    return next((name for name, pattern in SKIPPABLE if pattern.search(text)), None)


def text_layer(body):
    """Return whether a PDF is ``text``, ``low`` (little text) or ``scanned``, from up to 20 pages, or None."""
    try:
        with pymupdf.open(stream=body, filetype="pdf") as pdf:
            pages = min(len(pdf), TEXT_LAYER_PAGES)
            if not pages:
                return None
            chars = sum(len(pdf[i].get_text().strip()) for i in range(pages)) / pages
    except Exception:  # noqa: BLE001 # a damaged or encrypted PDF has no measurable text layer
        return None
    return "scanned" if chars < SCANNED_CHARS_PER_PAGE else "low" if chars < LOW_TEXT_CHARS_PER_PAGE else "text"


def zip_directory(tail):
    """Return the offset and length of a ZIP file's member list, from its last bytes, or None (including ZIP64)."""
    end = tail.rfind(b"PK\x05\x06")
    if end < 0 or len(tail) - end < 22:  # noqa: PLR2004 # the end-of-central-directory record's length
        return None
    length, offset = struct.unpack("<II", tail[end + 12 : end + 20])
    return None if offset == ZIP64_OFFSET else (offset, length)


def zip_name(raw, flags, extra=b""):
    """Decode a ZIP member's name from its bytes, flags and extra field. Every reader of the stores should use this."""
    if flags & 0x800:
        return raw.decode("utf-8", "replace")
    position = 0
    while position + 4 <= len(extra):
        kind, length = struct.unpack("<HH", extra[position : position + 4])
        data = extra[position + 4 : position + 4 + length]
        # An Info-ZIP Unicode Path field applies only if written for this name (version 1, with the name's CRC). Some
        # archivers fill it with the name's bytes read as CP437, which is wrong for Polish names.
        if kind == 0x7075 and len(data) > 5 and data[0] == 1 and data[1:5] == struct.pack("<I", zlib.crc32(raw)):  # noqa: PLR2004
            try:
                name = data[5:].decode("utf-8")
            except UnicodeDecodeError:
                break
            if name != raw.decode("cp437"):
                return name.replace("\\", "/")
            break
        position += 4 + length
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        # Polish Windows archivers write names in code page 852 without setting the UTF-8 flag.
        return raw.decode("cp852")


def zip_info_name(info):
    """Decode a ``zipfile.ZipInfo``'s name with ``zip_name``."""
    return zip_name(
        info.orig_filename.encode("utf-8" if info.flag_bits & 0x800 else "cp437"), info.flag_bits, info.extra
    )


def zip_entries(directory):
    """Return ``{"name", "size", "crc", "dir"}`` for each member in a ZIP file's member list."""
    entries, position = [], 0
    while directory[position : position + 4] == b"PK\x01\x02":
        flags, crc, size, name_length, extra_length, comment_length, external = struct.unpack(
            "<8xH6xI4xIHHH4xI", directory[position : position + 42]
        )
        start = position + 46
        raw = directory[start : start + name_length]
        extra = directory[start + name_length : start + name_length + extra_length]
        i = 0
        while size == ZIP64_OFFSET and i + 12 <= len(extra):
            kind, length = struct.unpack("<HH", extra[i : i + 4])
            if kind == 1:  # ZIP64 extended information, whose first field is the size
                size = struct.unpack("<Q", extra[i + 4 : i + 12])[0]
            i += 4 + length
        entries.append(
            {
                "name": zip_name(raw, flags, extra),
                "size": size,
                "crc": f"{crc:08X}",
                "dir": raw.endswith((b"/", b"\\")) or bool(external & 0x10) or bool(external >> 16 & 0o040000),
            }
        )
        position = start + name_length + extra_length + comment_length
    return entries


def zip_members(directory):
    """Return ``[name, size]`` for each member in a ZIP file's member list."""
    return [[entry["name"], entry["size"]] for entry in zip_entries(directory)]


def zip_set_entries(paths, *, spanned=False):
    """
    Return ``zip_entries`` for a ZIP file, or a split set's parts in order.

    A spanned set (.z01, .z02, ..., .zip) has a disk per part. A numbered set (.zip.001, .zip.002, ...) is one file cut
    into parts.
    """
    sizes = [path.stat().st_size for path in paths]

    def read(disk, offset, length):
        if not spanned:
            disk = 0
            while disk < len(sizes) - 1 and offset >= sizes[disk]:
                offset -= sizes[disk]
                disk += 1
        chunks = []
        while length > 0 and disk < len(paths):
            with paths[disk].open("rb") as f:
                f.seek(offset)
                chunks.append(f.read(length))
            length -= len(chunks[-1])
            disk, offset = disk + 1, 0
        return b"".join(chunks)

    last, end = (len(paths) - 1, sizes[-1]) if spanned else (0, sum(sizes))
    start = max(0, end - 65557)  # the end record, with a comment of up to 64 KiB
    tail = read(last, start, end - start)
    at = tail.rfind(b"PK\x05\x06")
    if at < 0:
        raise ValueError("no end of central directory record")
    disk, count, length, offset = struct.unpack("<2xH2xHII", tail[at + 4 : at + 20])
    if ZIP64_OFFSET in {length, offset} or 0xFFFF in {disk, count}:  # noqa: PLR2004
        if tail[at - 20 : at - 16] != b"PK\x06\x07":
            raise ValueError("no ZIP64 end of central directory locator")
        record_disk, record_offset = struct.unpack("<IQ", tail[at - 16 : at - 4])
        record = read(record_disk, record_offset, 56)
        if record[:4] != b"PK\x06\x06":
            raise ValueError("no ZIP64 end of central directory record")
        disk, count, length, offset = struct.unpack("<20xI8xQQQ", record)
    entries = zip_entries(read(disk, offset, length))
    if len(entries) != count:
        raise ValueError(f"{len(entries)} members in the member list, expected {count}")
    return entries


def host(url):
    return urlparse(url if "://" in url else f"https://{url}").netloc.lower().split(":")[0].removeprefix("www.")


def parse_size(text):
    """Parse a file size like ``731 KB`` or ``0.64 MB`` to bytes."""
    if match := re.search(r"([\d.,]+)\s*(B|KB|MB|GB)\b", text or "", re.IGNORECASE):
        return int(
            float(match.group(1).replace(",", ".")) * 1024 ** ["B", "KB", "MB", "GB"].index(match.group(2).upper())
        )
    return None


def compact(text):
    return re.sub(r"\s+", "", text or "").lower().replace("\u2013", "-").replace("\u2014", "-")


def words_in_order(reference, number):
    """Return whether the reference's words occur in order among the number's, like "EZP 74/22" in "EZP nr 74/22"."""
    words = iter(re.findall(r"\w+", number.lower()))
    return all(word in words for word in re.findall(r"\w+", reference.lower()))


def alphanumeric(text):
    return re.sub(r"\W+", "", text or "").lower()


def parse_datetime(value):
    """Parse an ISO 8601 datetime, or a TED date like ``2026-03-09+01:00``. Times without offsets are Polish."""
    if not value:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2}(?::\d{2})?)?", value):
        return dt.datetime.fromisoformat(value).replace(tzinfo=WARSAW)
    value = value.replace("Z", "+00:00")
    if re.match(r"\d{4}-\d{2}-\d{2}[+-]", value):
        value = f"{value[:10]}T23:59:59{value[10:]}"
    return dt.datetime.fromisoformat(value)


def same_buyer(names, others):
    """Return whether any buyer name shares two words, or all of the shorter name's words, with any other name."""
    for name in names:
        for other in others:
            a, b = buyer_words(name), buyer_words(other)
            if a and b and len(a & b) >= min(2, len(a), len(b)):
                return True
    return False


def buyer_words(name):
    # Five-letter prefixes match inflected forms, like "Ostrołęka" and "w Ostrołęce".
    text = unicodedata.normalize("NFC", html.unescape(name or "")).lower().translate(FOLD)
    return {word[:5] for word in re.findall(r"\w{3,}", text) if word not in BUYER_STOPWORDS}


def write_bytes(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)


def normalize_url(url):
    """Unescape HTML entities, strip trailing punctuation and correct misspelled hosts in a notice's documents URL."""
    url = html.unescape(url).rstrip(".,;)")
    for typo, correct in HOST_TYPOS.items():
        url = re.sub(rf"^(?:https?://)?{re.escape(typo)}", f"https://{correct}", url)
    return url


def reference_numbers(text):
    """Split a reference like "nr z Platformy: 9090/IREZA3/02597/01189/23/P; nr sprawy: PZ.292.436.2023"."""
    return [number for part in text.split(";") if (number := part.rsplit(":", 1)[-1].strip())]


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
