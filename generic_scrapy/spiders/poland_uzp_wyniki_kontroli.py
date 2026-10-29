__review_status__ = "unreviewed"
__generated_by__ = "claude-code/claude-opus-5-5"
__generated_at__ = "2026-10-28"

import csv
import json
import re
import subprocess
from pathlib import Path

import pymupdf
import scrapy
from scrapy.utils.defer import maybe_deferred_to_future
from scrapy.utils.project import data_path
from twisted.internet.threads import deferToThread

from generic_scrapy.base_spiders.base_spider import BaseSpider

# A text layer that is shorter than this, or in which fewer letters than this ratio have diacritics, is unusable.
# Some PDFs' fonts lack a Unicode map for Polish letters, so every word containing one is dropped.
MIN_TEXT_LENGTH = 200
MIN_DIACRITIC_RATIO = 0.025
OCR_DPI = 200

CONTROL_ID_RE = re.compile(r"(?<![A-Za-z])W?(KUD|KND|KU)[\s_\-]*(\d+)(?:[\s_\-]*(20\d\d|\d\d)(?!\d))?", re.IGNORECASE)

OBJECTIONS_RE = re.compile(
    r"zastrze[zż]e|ostateczn\w* (?:rozpatrz|rozstrzygn|wynik)|odniesieni\w* si[eę] do|odp\w* na zastrz", re.IGNORECASE
)
RESULT_RE = re.compile(r"info\w* o wyniku|protok[oó][lł] kontroli", re.IGNORECASE)
# Matched against the title and filename (with underscores as spaces), then the opening of the text. Order matters.
TITLE_KINDS = (
    ("not_controlled", re.compile(r"nie podlega kontroli", re.IGNORECASE)),
    ("kio_resolution", re.compile(r"uchwał|\bKIO\b", re.IGNORECASE)),
    ("recommendations_confirmation", re.compile(r"potwierdzeni", re.IGNORECASE)),
    ("correction", re.compile(r"sprostowani", re.IGNORECASE)),
    ("annex", re.compile(r"\bza[lł](?:\b|[aą]cznik)|subsydi", re.IGNORECASE)),
    ("objections_ruling", OBJECTIONS_RE),
    ("result", RESULT_RE),
)
TEXT_KINDS = (
    # A result's heading. It can follow a cover letter that mentions objections.
    ("result", re.compile(r"Informacja o wyniku kontroli (?:uprzedniej|doraźnej)")),
    ("recommendations_confirmation", re.compile(r"potwierdz\w* wykonani\w* zalece", re.IGNORECASE)),
    ("correction", re.compile(r"sprostowani", re.IGNORECASE)),
    ("annex", re.compile(r"^.{0,100}\bZałącznik nr|art\. 617e|subsydi", re.IGNORECASE)),
    ("objections_ruling", OBJECTIONS_RE),
    ("result", RESULT_RE),
    ("result", re.compile(r"stwierdz\w* narusze|Przedmiot zamówienia", re.IGNORECASE)),
)

# Header fields, matched against the first 8000 characters of the whitespace-normalised text.
FIELDS = {
    "authority": r"Zamawiaj\w+:\s*(.+?)\s+(?:ul\.|al\.|Al\.|Rodzaj|pl\.|\d{2}-\d{3})",
    "contract_type": r"Rodzaj zamówienia:\s*(dostaw\w*|usług\w*|robot\w* budowlan\w*)",
    "subject": r"Przedmiot zamówienia:\s*(.+?)\s+(?:Tryb|Rodzaj procedury|Data wszcz)",
    "procedure_type": r"Tryb (?:postępowania|udzielenia zamówienia):\s*(.+?)\s+(?:Data|Dat |Numer|Wartość)",
    "procedure_start": r"Dat\w* wszczęcia\s*postępowania:\s*(\d{1,2}[.\s]\d{1,2}[.\s]\d{4})",
    "notice": r"Numer\w* ogłosze\w*(?: o zamówieniu)?:\s*(.+?)\s+(?:Wartość|Środki|telefon|Szacunkowa)",
    "value": r"Wartość zamówienia:\s*([\d\s.,]+ ?zł)",
    "result_date": r"Warszawa,\s*(?:dnia\s*)?(\d{1,2}[.\s]\w+[.\s]\d{4})",
}
FIELDS = {key: re.compile(pattern) for key, pattern in FIELDS.items()}

# Section 2 of a result. Notice numbers are read from the header before it.
# Usually "2. Informacja o stwierdzeniu naruszeń lub ich braku". OCR can drop the number.
SECTION_2_RE = re.compile(r"(?:\d\.\s*)?Informacja o stwierdz(?:eniu|onych) narusze")
# The end of the header, in which notice numbers are searched. Older layouts lack it.
HEADER_END_RE = re.compile(r"2\.\s*Informacja o stwierdzeniu")
# "barak" is a typo in KU/96/25.
NO_BREACH_RE = re.compile(r"ba?rak\w* (?:stwierdzonych )?narusze|nie stwierdz\w* narusze", re.IGNORECASE)
BREACH_RE = re.compile(r"narusz", re.IGNORECASE)

NOTICE_RE = re.compile(
    r"20\d\d/[S$] \d{1,3}\s*-\s*\d{5,7}|20\d\d/BZP \d{6,8}(?:/\d{2})?|\d{6}-N-20\d\d|\b\d{5,8}\s*-\s*20\d\d\b"
)
# Same normalisation as oecd-poland's poc/build_labels.py.
NOTICE_PATTERNS = (
    # BZP, e.g. "2024/BZP 00354990/01"
    (
        re.compile(r"(\d{4})/BZP\s*(\d+)(?:/(\d+))?"),
        lambda m: f"{m[1]}/BZP {int(m[2]):08d}" + (f"/{m[3]}" if m[3] else ""),
    ),
    # TED OJ S, e.g. "2023/S 055-162109". OCR can read S as $.
    (
        re.compile(r"(\d{4})\s*/\s*[S$]\s*(\d+)\s*[-\u2013]\s*(\d+)"),
        lambda m: f"{m[1]}/S {int(m[2]):03d}-{int(m[3]):06d}",
    ),
    # Pre-2021 BZP, e.g. "589761-N-2020"
    (re.compile(r"\b(\d+)-N-(\d{4})\b"), lambda m: f"{m[1]}-N-{m[2]}"),
    # TED eForms, e.g. "718430-2025" or "659230 - 2024"
    (re.compile(r"(?<![\d/-])(\d{1,8})\s*[-\u2013]\s*(20\d\d)\b"), lambda m: f"{int(m[1])}-{m[2]}"),
)

BREACH_OPEN_RE = re.compile(
    # "stwierdza: PREZES URZĘDU ZAMÓWIEŃ PUBLICZNYCH 2 1. naruszenie" spans a page break.
    r"stwierdz(?:a|ił|ono)\W{0,3}(?:\S+\s+){0,6}?naruszeni|informuje o (?:stwierdzon|wykazan)\w* naruszeni"
    r"|informuje o stwierdzeniu\b(?! naruszeń lub)|stwierdzenia naruszeni",
    re.IGNORECASE,
)
BREACH_CLOSE_RE = re.compile(
    r"UZASADNIENIE|Uzasadnienie|Pismem z dnia|Zamawiający wniósł|W zastrzeżeniach|Odnosząc się|POUCZENIE"
)
BREACH_MAX_LENGTH = 4000
CITE_RE = re.compile(
    r"art\.\s*(\d+)((?:\s*(?:ust\.|pkt|lit\.)\s*[\w\-\u2013]+(?:\s*(?:i|oraz|,)\s*(?:ust\.\s*)?[\d\-\u2013]+)*)*)",
    re.IGNORECASE,
)
# A citation preceded by one of these describes the breach rather than being the breached article.
REFERENCE_RE = re.compile(
    r"(mowa w|określon\w* w|zgodnie z|na podstawie|wymienion\w* w|wskazan\w* w)\s*$", re.IGNORECASE
)
# Art. 596-620 are the control provisions themselves.
CONTROL_ARTICLES = range(596, 621)
OTHER_STATUTE_RE = re.compile(
    r"\s*(?:§\s*\d+\s*)?(?:ustawy z dnia (?:16 kwietnia|13 kwietnia|1 marca|23 kwietnia)|ustawy Znk"
    r"|ustawy o (?:szczególnych|zwalczaniu)|u\.?\s?z\.?\s?n\.?\s?k|rozporządzenia|Kodeksu|k\.?\s?c\b)",
    re.IGNORECASE,
)
UST_RE = re.compile(r"ust\.\s*([\d\-\u2013]+(?:\s*(?:i|oraz|,)\s*(?:ust\.\s*)?[\d\-\u2013]+)*)")
PKT_RE = re.compile(r"pkt\s*(\d+)")
LAW_2004_RE = re.compile(r"29 stycznia 2004|Pzp\s*2004|ustawy z 2004")

# Columns of oecd-poland's uzp_manifest.csv, then this spider's additions.
CONTROL_COLUMNS = [
    "control_id",
    "kind",
    "year",
    "outcome",
    "authority",
    "contract_type",
    "subject",
    "procedure_type",
    "procedure_start",
    "notice",
    "notice_ids",
    "value",
    "result_date",
    "files",
    "urls",
    "file_kinds",
    "law",
    "breached_articles",
    "breach_statement",
]
EMPTY_FIELDS = {
    **dict.fromkeys(FIELDS),
    "notice_ids": [],
    "outcome": None,
    "breach_statement": None,
    "breached_articles": [],
    "law": None,
}


def flatten(text):
    return " ".join(text.replace("\u200b", "").split())


def control_ids(title, year):
    """Return the normalised control IDs in a file title, like ``KU_12_25``. Use ``year`` if the title omits it."""
    matches = list(CONTROL_ID_RE.finditer(title))
    # "uchwała KIO_KU_10_26_dot_KUD_1_26" is a KIO resolution about KUD_1_26.
    between = title[matches[0].end() : matches[1].start()] if len(matches) > 1 else ""
    if re.search(r"(?<![a-z])dot(?![a-z])", between, re.IGNORECASE):
        matches = matches[1:]
    ids = []
    for match in matches:
        number = str(int(match[2]))
        yy = match[3][-2:] if match[3] else str(year)[-2:]
        ids.append(f"{match[1].upper()}_{number}_{yy}")
    return list(dict.fromkeys(ids))


def file_kind(title, filename, flat):
    for haystack, kinds in ((f"{title} {filename}".replace("_", " "), TITLE_KINDS), (flat[:1500], TEXT_KINDS)):
        for kind, pattern in kinds:
            if pattern.search(haystack):
                return kind
    return "other"


def usable(text):
    letters = len(re.findall(r"[^\W\d_]", text))
    diacritics = len(re.findall(r"[ąćęłńóśźżĄĆĘŁŃÓŚŹŻ]", text))
    return len(flatten(text)) >= MIN_TEXT_LENGTH and diacritics >= MIN_DIACRITIC_RATIO * letters


def parse_notices(text):
    """Return the normalised notice numbers in free text."""
    found = set()
    for pattern, fmt in NOTICE_PATTERNS:
        found |= {fmt(m) for m in pattern.finditer(text)}
        text = pattern.sub(" ", text)
    return found


def outcome(flat):
    breach = BREACH_OPEN_RE.search(flat)
    if match := SECTION_2_RE.search(flat):
        section = flat[match.end() : match.end() + 1500]
        if NO_BREACH_RE.search(section):
            return "no_breach"
        if BREACH_RE.search(section) or breach:
            return "breach"
        return None
    # An objections ruling can quote the authority's claim of "brak naruszenia" after restating the breach.
    head = flat[:6000]
    no_breach = NO_BREACH_RE.search(head)
    if no_breach and (not breach or no_breach.start() < breach.start()):
        return "no_breach"
    if breach or BREACH_RE.search(head):
        return "breach"
    return None


def expand(numbers):
    out = []
    for raw in re.split(r"\s*(?:i|oraz|,)\s*", numbers):
        part = raw.replace("ust.", "").strip()
        if match := re.match(r"(\d+)\s*[\-\u2013]\s*(\d+)$", part):
            out.extend(str(i) for i in range(int(match[1]), int(match[2]) + 1))
        elif re.match(r"\d+$", part):
            out.append(part)
    return out


def breach_statement(flat):
    if not (match := BREACH_OPEN_RE.search(flat)):
        return None
    end = BREACH_CLOSE_RE.search(flat, match.end())
    return flat[match.start() : min(end.start() if end else len(flat), match.end() + BREACH_MAX_LENGTH)].strip()


def breached_articles(statement):
    """Return the Pzp articles cited in a breach statement, like ``art. 99 ust. 4``."""
    articles = []
    for match in CITE_RE.finditer(statement):
        if REFERENCE_RE.search(statement[max(0, match.start() - 40) : match.start()]):
            continue
        article = int(match[1])
        if article in CONTROL_ARTICLES or OTHER_STATUTE_RE.match(statement, match.end()):
            continue
        tail = match[2] or ""
        ust = UST_RE.search(tail)
        pkt = PKT_RE.search(tail)
        for u in expand(ust[1]) if ust else [""]:
            citation = f"art. {article}"
            if u:
                citation += f" ust. {u}"
            if pkt:
                citation += f" pkt {pkt[1]}"
            articles.append(citation)
    return list(dict.fromkeys(articles))


def parse_text(flat):
    """Return the fields parsed from a control result's text. Missing fields are ``None``."""
    head = flat[:8000]
    fields = {}
    for key, pattern in FIELDS.items():
        match = pattern.search(head)
        fields[key] = " ".join(match[1].split())[:300] if match else None
    # The label and number are often split by OCR or layout, so search the whole header.
    match = HEADER_END_RE.search(flat)
    header = flat[: match.start()] if match else flat[:4000]
    fields["notice_ids"] = sorted(parse_notices(" ; ".join(NOTICE_RE.findall(header))))
    fields["outcome"] = outcome(flat)
    fields["breach_statement"] = statement = breach_statement(flat) if fields["outcome"] != "no_breach" else None
    fields["breached_articles"] = breached_articles(statement) if statement else []
    fields["law"] = "2004" if LAW_2004_RE.search(flat) else "2019"
    return fields


def ocr(body, lang="pol"):
    """Return the text of a scanned PDF, by running tesseract on each page's render."""
    pages = []
    with pymupdf.open(stream=body, filetype="pdf") as document:
        for page in document:
            png = page.get_pixmap(dpi=OCR_DPI).tobytes("png")
            result = subprocess.run(  # noqa: S603 # constant arguments
                ["tesseract", "stdin", "stdout", "-l", lang],  # noqa: S607 # on PATH
                input=png,
                capture_output=True,
                check=True,
            )
            pages.append(result.stdout.decode())
    return "\n".join(pages)


class PolandUzpWynikiKontroli(BaseSpider):
    """
    UZP control results (Informacja o wyniku kontroli) from gov.pl/web/uzp, ex-ante 2023 onward, ad-hoc 2021 onward.

    Walks the year pages under the ex-ante and ad-hoc parent pages, downloads every file and extracts its text with
    PyMuPDF (and tesseract, with ``-a ocr=true``). Writes one JSONL row per file and control to ``files.json``, and
    a per-control rollup to ``controls.json`` and ``controls.csv``. Saves the PDFs and texts under ``files/``.
    """

    name = "poland_uzp_wyniki_kontroli"

    start_urls_by_kind = {
        "ex-ante": "https://www.gov.pl/web/uzp/kontrole-uprzednie",
        "ad-hoc": "https://www.gov.pl/web/uzp/kontrole-od-2021-roku",
    }

    # BaseSpider
    export_name = "files"

    custom_settings = {
        "CONCURRENT_REQUESTS_PER_DOMAIN": 4,
        "DOWNLOAD_DELAY": 0.25,
        "HTTPCACHE_ENABLED": True,
        "HTTPCACHE_DIR": "data/poland_uzp_wyniki_kontroli/_httpcache",
        "HTTPCACHE_EXPIRATION_SECS": 0,
        "HTTPCACHE_IGNORE_HTTP_CODES": [404, 500, 502, 503],
        "DOWNLOAD_MAXSIZE": 64 * 1024 * 1024,
    }

    def __init__(self, *args, ocr="false", **kwargs):
        super().__init__(*args, **kwargs)
        self.ocr = ocr.lower() in {"true", "1", "yes"}
        self.queued = 0
        self.rows = []

    async def start(self):
        for kind, url in self.start_urls_by_kind.items():
            # The year pages can change, so don't cache the listings.
            yield scrapy.Request(url, callback=self.parse_parent, cb_kwargs={"kind": kind}, meta={"dont_cache": True})

    def parse_parent(self, response, kind):
        for link in response.xpath("//article//div[contains(@class, 'art-prev')]//a"):
            text = " ".join(link.xpath(".//text()").getall())
            if match := re.fullmatch(r"\s*Rok (20\d\d)\s*", text):
                yield response.follow(
                    link,
                    callback=self.parse_year,
                    cb_kwargs={"kind": kind, "year": int(match[1])},
                    meta={"dont_cache": True},
                )

    def parse_year(self, response, kind, year):
        for link in response.xpath("//article//a[contains(@class, 'file-download')]"):
            if self.sample and self.queued >= self.sample:
                return
            self.queued += 1
            url = response.urljoin(link.xpath("./@href").get())
            yield scrapy.Request(
                url,
                callback=self.parse_file,
                cb_kwargs={
                    "kind": kind,
                    "year": year,
                    "title": flatten(link.xpath("./text()").get() or ""),
                    "filename": flatten(link.xpath("./span[@class='extension']/text()").get() or ""),
                    "size": flatten(link.xpath("./span[@class='details']/text()").get() or ""),
                },
            )

    async def parse_file(self, response, kind, year, title, filename, size):
        uuid = response.url.rstrip("/").rsplit("/", 1)[-1]
        # PyMuPDF and tesseract block, so run them outside the reactor.
        text, text_status = await maybe_deferred_to_future(deferToThread(self.extract, uuid, response.body))

        flat = flatten(text)
        fields = parse_text(flat) if text_status != "empty" else EMPTY_FIELDS

        ids = control_ids(title, year) or control_ids(filename, year)
        if not ids:
            self.logger.warning("No control ID: %s (%s)", title, response.url)
            ids = [None]
        for control_id in ids:
            row = {
                "control_id": control_id,
                "kind": kind,
                "year": year,
                "title": title,
                "filename": filename,
                "url": response.url,
                "size": size,
                "file_kind": file_kind(title, filename, flat),
                "text_status": text_status,
                "path": f"files/{uuid}.pdf",
                **fields,
            }
            self.rows.append(row)
            yield row

    def extract(self, uuid, body):
        """Save the PDF and its text, and return the text and its ``text_status``."""
        directory = Path(self.settings["FILES_STORE"], self.name, self.crawl_directory, "files")
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{uuid}.pdf").write_bytes(body)

        try:
            with pymupdf.open(stream=body, filetype="pdf") as document:
                text = "\n".join(page.get_text() for page in document)
        except (pymupdf.FileDataError, RuntimeError):
            self.logger.warning("Not a PDF: %s", uuid)
            text = ""
        text_status = "text"
        if not usable(text):
            text_status = "empty"
            if self.ocr and usable(ocr_text := self.ocr_text(uuid, body)):
                text, text_status = ocr_text, "ocr"
        (directory / f"{uuid}.txt").write_text(text, encoding="utf-8")
        return text, text_status

    def ocr_text(self, uuid, body):
        """Return the OCR text of a PDF, from a cache that persists across crawls."""
        path = Path(data_path(f"data/{self.name}/_ocrcache", createdir=True), f"{uuid}.txt")
        if path.exists():
            return path.read_text(encoding="utf-8")
        self.logger.info("OCR %s", uuid)
        text = ocr(body)
        path.write_text(text, encoding="utf-8")
        return text

    def closed(self, reason):
        controls = rollup(self.rows)
        directory = Path(self.settings["FILES_STORE"], self.name, self.crawl_directory)
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / "controls.json").open("w", encoding="utf-8") as f:
            for control in controls:
                f.write(json.dumps(control, ensure_ascii=False) + "\n")
        with (directory / "controls.csv").open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CONTROL_COLUMNS)
            writer.writeheader()
            for control in controls:
                writer.writerow({k: "; ".join(v) if isinstance(v, list) else v for k, v in control.items()})
        self.logger.info("Wrote %d controls (%s)", len(controls), reason)


def rollup(rows):
    """Return one row per control. Header fields come from result files first, then the control's other files."""
    groups = {}
    for row in rows:
        if row["control_id"]:
            groups.setdefault(row["control_id"], []).append(row)

    priority = {"result": 0, "not_controlled": 1, "correction": 2, "objections_ruling": 3}
    controls = []
    for control_id, files in sorted(groups.items(), key=lambda item: sort_key(item[0])):
        ordered = sorted(files, key=lambda row: priority.get(row["file_kind"], 9))
        control = {
            "control_id": control_id,
            "kind": files[0]["kind"],
            "year": files[0]["year"],
        }
        for key in [*FIELDS, "law"]:
            control[key] = next((row[key] for row in ordered if row[key]), None)

        kinds = {row["file_kind"] for row in files}
        results = [row for row in files if row["file_kind"] == "result" and row["outcome"]]
        if "not_controlled" in kinds:
            control["outcome"] = "not_controlled"
        elif results:
            # A control split into parts has one result per part.
            outcomes = {row["outcome"] for row in results}
            control["outcome"] = "breach" if "breach" in outcomes else "no_breach"
        else:
            control["outcome"] = next((row["outcome"] for row in ordered if row["outcome"]), None)

        control["notice_ids"] = sorted({n for row in files for n in row["notice_ids"]})
        control["files"] = [row["title"] for row in files]
        control["urls"] = [row["url"] for row in files]
        control["file_kinds"] = [row["file_kind"] for row in files]
        control["breached_articles"] = list(
            dict.fromkeys(a for row in ordered if row["file_kind"] != "annex" for a in row["breached_articles"])
        )
        control["breach_statement"] = "\n\n".join(
            row["breach_statement"] for row in ordered if row["file_kind"] == "result" and row["breach_statement"]
        ) or next((row["breach_statement"] for row in ordered if row["breach_statement"]), None)
        controls.append({column: control.get(column) for column in CONTROL_COLUMNS})
    return controls


def sort_key(control_id):
    prefix, number, year = control_id.split("_")
    return prefix, int(year), int(number)
