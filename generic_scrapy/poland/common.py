__review_status__ = "unreviewed"
__generated_by__ = "claude-code/claude-opus-5-5"
__generated_at__ = "2026-11-21"

import datetime as dt
import html
import json
import re
import unicodedata
import zoneinfo
from urllib.parse import urlparse

HTML_PAGE_ERROR = "HTML page instead of the file"
UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
TENDER_ID_RE = re.compile(r"ocds-148610-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# Hosts misspelled in notices' documents URLs.
HOST_TYPOS = {
    "platformazakupowaz.plk-sa.pl": "platformazakupowa.plk-sa.pl",
    "www.platformazakupowa-plk-sa.pl": "platformazakupowa.plk-sa.pl",
}
WARSAW = zoneinfo.ZoneInfo("Europe/Warsaw")

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


def normalize_space(text):
    return " ".join((text or "").split())


def node_text(selector):
    return normalize_space(" ".join(selector.xpath(".//text()").getall()))


def role(*names):
    """Return the document's role (swz, opz, ppu, annex, swz_change, qa, appeal, notice, notice_change or other)."""
    text = " ".join(normalize_space(name).lower().replace("_", " ") for name in names if name)
    text = unicodedata.normalize("NFC", text).translate(FOLD)
    return next((name for name, pattern in ROLES if pattern.search(text)), "other")


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
