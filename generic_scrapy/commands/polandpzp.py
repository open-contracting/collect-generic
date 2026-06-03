import csv
import json
import logging
import re
from collections import Counter
from pathlib import Path

from scrapy.commands import ScrapyCommand
from scrapy.exceptions import UsageError

logger = logging.getLogger(__name__)

# Captures the four-level reference structure used in Pzp citations:
#   art. <article>[ ust. <ust>][ pkt <pkt-expression>][ lit. <lit>]
# - article: digits with an optional lower-case letter suffix ("22d" is a distinct article).
# - ust / pkt: digits with an optional letter suffix ("ust. 1a", "pkt 7b").
# - pkt-expression: one value, a range ("1-3"), a comma list ("1, 2, 3"),
#   the Polish "i" connector ("1 i 2"), or any mixture ("1, 2-4 i 6").
# - lit: a single lower-case letter.
DASHES = "[-\u2013\u2014]"  # hyphen-minus, en-dash (U+2013), em-dash (U+2014)
PKT_VALUE = r"\d+[a-z]?"
PKT_CONNECTOR = r"(?:\s*,\s*|\s+i\s+|\s*" + DASHES + r"\s*)"
SUBCLAUSE_RE = re.compile(
    r"art\.\s*(?P<article>\d+[a-z]?)"
    r"(?:\s*ust\.?\s*(?P<ust>\d+[a-z]?))?"
    r"(?:\s*pkt\.?\s*(?P<pkt>" + PKT_VALUE + r"(?:" + PKT_CONNECTOR + PKT_VALUE + r")*))?"
    r"(?:\s*lit\.?\s*(?P<lit>[a-z]))?",
    re.IGNORECASE,
)
PKT_RANGE_RE = re.compile(r"^\s*(\d+)\s*" + DASHES + r"\s*(\d+)\s*$")
PKT_LIST_SPLIT_RE = re.compile(r"\s*,\s*|\s+i\s+")


def _expand_pkt(pkt):
    """
    Yield every pkt value referenced by ``pkt``.

    Handles single values ("1", "7b"), ranges ("1-3"), comma lists ("1, 2, 3"),
    the Polish "i" connector ("1 i 2"), and mixtures ("1, 2-4 i 6"). Malformed
    ranges (start > end) pass through unchanged.
    """
    if not pkt:
        yield ""
        return
    for raw in PKT_LIST_SPLIT_RE.split(pkt):
        part = raw.strip()
        if not part:
            continue
        match = PKT_RANGE_RE.match(part)
        if not match:
            yield part
            continue
        start, end = int(match.group(1)), int(match.group(2))
        if start > end:
            yield part
            continue
        for n in range(start, end + 1):
            yield str(n)


class PolandPzp(ScrapyCommand):
    def short_desc(self):
        return (
            "Aggregate PZP article counts from the latest kio_orzeczenia and uzp_kontrole crawls. "
            "Writes pzp_article_counts.csv, pzp_subclause_counts.csv and uzp_category_breakdown.csv."
        )

    def syntax(self):
        return "[options]"

    def add_options(self, parser):
        ScrapyCommand.add_options(self, parser)
        parser.add_argument(
            "--output-dir",
            type=str,
            default=".",
            help="Directory to write the CSV outputs to (default: cwd)",
        )
        parser.add_argument(
            "--kio-crawl",
            type=str,
            help="kio_orzeczenia crawl_directory (default: latest under FILES_STORE/kio_orzeczenia/)",
        )
        parser.add_argument(
            "--uzp-crawl",
            type=str,
            help="uzp_kontrole crawl_directory (default: latest under FILES_STORE/uzp_kontrole/)",
        )

    def run(self, _args, opts):
        files_store = Path(self.settings["FILES_STORE"])
        kio_crawl = self._resolve_crawl(files_store, "kio_orzeczenia", opts.kio_crawl)
        uzp_crawl = self._resolve_crawl(files_store, "uzp_kontrole", opts.uzp_crawl)
        if not kio_crawl and not uzp_crawl:
            raise UsageError(f"No crawls found under {files_store}/kio_orzeczenia/ or {files_store}/uzp_kontrole/")

        article_counts: Counter[tuple[str, str]] = Counter()
        subclause_counts: Counter[tuple[str, str, str, str, str]] = Counter()
        category_counts: Counter[str] = Counter()
        totals: dict[str, int] = {"KIO": 0, "UZP": 0}

        if kio_crawl:
            for row in _read_jsonl(kio_crawl / "rulings.json"):
                totals["KIO"] += 1
                _consume("KIO", row.get("articles") or [], article_counts, subclause_counts)

        if uzp_crawl:
            for row in _read_jsonl(uzp_crawl / "findings.json"):
                totals["UZP"] += 1
                _consume("UZP", [row.get("violation_text") or ""], article_counts, subclause_counts)
                if row.get("category"):
                    category_counts[row["category"]] += 1

        output_dir = Path(opts.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        articles_path = output_dir / "pzp_article_counts.csv"
        with articles_path.open("w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["article", "source", "count", "share_within_source"])
            for (article, source), count in sorted(article_counts.items(), key=_article_sort_key):
                share = count / totals[source] if totals[source] else 0
                writer.writerow([article, source, count, f"{share:.4f}"])

        subclauses_path = output_dir / "pzp_subclause_counts.csv"
        with subclauses_path.open("w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["article", "ust", "pkt", "lit", "source", "count", "share_within_source"])
            for (article, ust, pkt, lit, source), count in sorted(subclause_counts.items(), key=_subclause_sort_key):
                share = count / totals[source] if totals[source] else 0
                writer.writerow([article, ust, pkt, lit, source, count, f"{share:.4f}"])

        category_path = output_dir / "uzp_category_breakdown.csv"
        with category_path.open("w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["category", "count", "share"])
            for category, count in category_counts.most_common():
                share = count / totals["UZP"] if totals["UZP"] else 0
                writer.writerow([category, count, f"{share:.4f}"])

        logger.info("KIO rulings:  %6d   from %s", totals["KIO"], kio_crawl)
        logger.info("UZP findings: %6d   from %s", totals["UZP"], uzp_crawl)
        logger.info("Wrote %s (%d article/source rows)", articles_path, len(article_counts))
        logger.info("Wrote %s (%d sub-clause/source rows)", subclauses_path, len(subclause_counts))
        logger.info("Wrote %s (%d categories)", category_path, len(category_counts))

    @staticmethod
    def _resolve_crawl(files_store, spider_name, override):
        spider_dir = files_store / spider_name
        if override:
            crawl = spider_dir / override
            if not crawl.is_dir():
                raise UsageError(f"crawl_directory not found: {crawl}")
            return crawl
        if not spider_dir.is_dir():
            return None
        crawls = sorted(p for p in spider_dir.iterdir() if p.is_dir())
        return crawls[-1] if crawls else None


def _read_jsonl(path):
    if not path.exists():
        return
    with path.open() as f:
        for raw_line in f:
            line = raw_line.strip()
            if line:
                yield json.loads(line)


def _consume(source, texts, article_counts, subclause_counts):
    seen_articles = set()
    seen_subclauses = set()
    for text in texts:
        if not text:
            continue
        for match in SUBCLAUSE_RE.finditer(text):
            article = match["article"]
            ust = match["ust"] or ""
            lit = match["lit"] or ""
            if article not in seen_articles:
                article_counts[(article, source)] += 1
                seen_articles.add(article)
            # A pkt range like "1-3" expands to one sub-clause row per integer it spans.
            for pkt in _expand_pkt(match["pkt"]):
                key = (article, ust, pkt, lit)
                if key not in seen_subclauses:
                    subclause_counts[(article, ust, pkt, lit, source)] += 1
                    seen_subclauses.add(key)


def _article_sort_key(kv):
    (article, source), count = kv
    return (-count, article, source)


def _subclause_sort_key(kv):
    (article, ust, pkt, lit, source), count = kv
    return (-count, article, ust, pkt, lit, source)
