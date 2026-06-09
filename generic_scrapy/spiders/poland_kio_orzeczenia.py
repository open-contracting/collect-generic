import datetime
import re
import ssl
import urllib.error
import urllib.request

import certifi
import scrapy
from scrapy.exceptions import CloseSpider

from generic_scrapy.base_spiders.export_file_spider import ExportFileSpider

CASE_YEAR_RE = re.compile(r"/(\d{2})\b")

# Starting guess for the binary-search upper bound. The /Home/Details/<id> id space is dense
# (~34010 as of 2026-06) and grows ~10k/year — auto-discovery walks past this if it's still valid.
DISCOVERY_START_GUESS = 35000

# Invalid IDs return a static 8406-byte "not found" page. Anything below this isn't a ruling.
ERROR_PAGE_MAX_LEN = 9000


class PolandKioOrzeczenia(ExportFileSpider):
    """
    Krajowa Izba Odwoławcza (KIO) rulings + KIO/KD control opinions from orzeczenia.uzp.gov.pl.

    Enumerates ``/Home/Details/<id>`` and writes one JSONL row per ruling with the metadata fields
    the search engine indexes — including the case number, outcome, contracting authority and the
    editorially-curated PZP articles cited (``Kluczowe przepisy ustawy Pzp``).

    IDs are not chronological, so the spider walks the full ID range and drops rulings older than
    ``from_date`` after parsing.
    """

    name = "poland_kio_orzeczenia"

    base_url = "https://orzeczenia.uzp.gov.pl/Home/Details"

    # ExportFileSpider
    export_outputs = {
        "main": {
            "name": "rulings",
            "formats": ["json"],
            "item_filter": None,
        },
    }

    # BaseSpider
    date_required = True
    default_from_date = "2023-01-01T00:00:00"

    def __init__(self, *args, max_id=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.max_id = int(max_id) if max_id else None
        self._yielded = 0

    @classmethod
    def from_crawler(cls, crawler, *args, **kwargs):
        spider = super().from_crawler(crawler, *args, **kwargs)
        if spider.max_id is None:
            user_agent = crawler.settings.get("USER_AGENT") or "poland_kio_orzeczenia"
            spider.logger.info("Discovering max Details/<id> via binary search...")
            spider.max_id = _discover_max_id(user_agent)
            spider.logger.info("Discovered max id: %d", spider.max_id)
        return spider

    async def start(self):
        # IDs are roughly chronological (most modern rulings at the top), so walking down from
        # max_id surfaces the post-2023 window first — important when --sample is set, and a
        # natural ordering even for a full crawl. start() is consumed lazily by the engine, so
        # raising CloseSpider in parse_detail stops the scheduler asking for more.
        for record_id in range(self.max_id, 0, -1):
            yield scrapy.Request(
                f"{self.base_url}/{record_id}",
                callback=self.parse_detail,
                cb_kwargs={"record_id": record_id},
            )

    def parse_detail(self, response, record_id):
        # The "not found" template is ~8406 bytes; real rulings are 10k+. Sygnatura akt is always
        # present on a real ruling.
        if len(response.body) < ERROR_PAGE_MAX_LEN:
            return
        sygnatura_raw = response.xpath(
            "//label[contains(normalize-space(.), 'Sygnatura akt')]/following-sibling::ul[1]/li[1]/text()"
        ).get()
        if not sygnatura_raw:
            return

        case_number, _, outcome = (s.strip() for s in sygnatura_raw.partition(" / "))
        date_iso = self._iso_date(self._field(response, "Metrics_IssueDate"))
        # Older rulings have no issue_date in the metadata; fall back to the year encoded in the
        # case number (e.g., "KIO 2650/15" → 2015). When neither is available, the ruling pre-dates
        # the current metadata schema and is dropped — those are almost always pre-2015.
        case_year_match = CASE_YEAR_RE.search(case_number)
        case_year = 2000 + int(case_year_match.group(1)) if case_year_match else None
        best_year = int(date_iso[:4]) if date_iso else case_year
        if not best_year or (self.from_date and best_year < self.from_date.year):
            return

        articles = []
        for text in response.xpath(
            "//b[normalize-space(.)='Kluczowe przepisy ustawy Pzp']/following-sibling::p[1]//a/text()"
        ).getall():
            for piece in text.split("|"):
                stripped = piece.strip()
                if stripped:
                    articles.append(stripped)

        self._yielded += 1
        if self.sample and self._yielded > self.sample:
            raise CloseSpider("sample limit reached")
        yield {
            "source": "KIO",
            "record_id": record_id,
            "case_number": case_number or None,
            "outcome": outcome.strip() or None,
            "document_type": self._field(response, "Metrics_DecisionType"),
            "issue_date": date_iso,
            "panel_chair": self._field(response, "Chairman"),
            "purchaser": self._field(response, "Purchaser"),
            "location": self._field(response, "City"),
            "procedure_type": self._field(response, "Procedure"),
            "contract_type": self._field(response, "ContractType"),
            "articles": articles,
            "url": response.url,
            "pdf_url": f"https://orzeczenia.uzp.gov.pl/Home/PdfContent/{record_id}?Kind=KIO",
        }

    @staticmethod
    def _field(response, label_for):
        # The metadata layout is `<label for="X">…</label><br …/> value </p>`. Grab the first text
        # node that follows the label inside the same <p>.
        text = response.xpath(f"//label[@for='{label_for}']/parent::p/text()[normalize-space()]").get()
        return text.strip() if text else None

    @staticmethod
    def _iso_date(text):
        if not text:
            return None
        try:
            return (
                datetime.datetime.strptime(text.strip(), "%d-%m-%Y")
                .replace(tzinfo=datetime.timezone.utc)
                .date()
                .isoformat()
            )
        except ValueError:
            return None


def _discover_max_id(user_agent, start_guess=DISCOVERY_START_GUESS, sanity_cap=1_000_000):
    """
    Return the highest /Home/Details/<id> that resolves to a real ruling.

    Issues at most ~log2(start_guess) + a few sequential HTTP requests via urllib (bypassing
    Scrapy's downloader so it can run during spider startup). Falls back to ``start_guess`` if the
    server is unreachable.
    """
    ssl_context = ssl.create_default_context(cafile=certifi.where())

    def is_valid(record_id):
        url = f"https://orzeczenia.uzp.gov.pl/Home/Details/{record_id}"
        request = urllib.request.Request(url, headers={"User-Agent": user_agent})  # noqa: S310
        try:
            with urllib.request.urlopen(request, timeout=15, context=ssl_context) as response:  # noqa: S310
                return len(response.read()) >= ERROR_PAGE_MAX_LEN
        except (urllib.error.URLError, TimeoutError):
            return False

    lo, hi = 1, start_guess
    # Expand the upper bound if start_guess is still valid.
    while is_valid(hi):
        lo = hi
        hi *= 2
        if hi > sanity_cap:
            return hi
    # Binary search the boundary: lo is valid, hi is invalid.
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if is_valid(mid):
            lo = mid
        else:
            hi = mid
    return lo
