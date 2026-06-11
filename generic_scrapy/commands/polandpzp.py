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
            "Aggregate PZP article and CPV counts from the latest poland_kio_orzeczenia, poland_uzp_kontrole and "
            "(optional) poland_cpv crawls. Writes pzp_article_counts.csv, pzp_subclause_counts.csv, "
            "uzp_category_breakdown.csv and (when poland_cpv is present) cpv_counts.csv "
            "and cpv_division_by_dimension.csv."
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
            help="poland_kio_orzeczenia crawl_directory (default: latest under FILES_STORE/poland_kio_orzeczenia/)",
        )
        parser.add_argument(
            "--uzp-crawl",
            type=str,
            help="poland_uzp_kontrole crawl_directory (default: latest under FILES_STORE/poland_uzp_kontrole/)",
        )
        parser.add_argument(
            "--cpv-crawl",
            type=str,
            help="poland_cpv crawl_directory (default: latest under FILES_STORE/poland_cpv/)",
        )

    def run(self, _args, opts):
        files_store = Path(self.settings["FILES_STORE"])
        kio_crawl = self._resolve_crawl(files_store, "poland_kio_orzeczenia", opts.kio_crawl)
        uzp_crawl = self._resolve_crawl(files_store, "poland_uzp_kontrole", opts.uzp_crawl)
        cpv_crawl = self._resolve_crawl(files_store, "poland_cpv", opts.cpv_crawl)
        if not kio_crawl and not uzp_crawl:
            raise UsageError(
                f"No crawls found under {files_store}/poland_kio_orzeczenia/ or {files_store}/poland_uzp_kontrole/"
            )

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

        if cpv_crawl:
            cpv_path = output_dir / "cpv_counts.csv"
            cpv_counts, cpv_labels, cpv_totals = _aggregate_cpv(cpv_crawl / "joined.json")
            with cpv_path.open("w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["cpv_code", "cpv_label", "source", "count", "share_within_source"])
                for (code, source), count in sorted(cpv_counts.items(), key=_article_sort_key):
                    share = count / cpv_totals[source] if cpv_totals[source] else 0
                    writer.writerow([code, cpv_labels.get(code, ""), source, count, f"{share:.4f}"])
            logger.info(
                "Wrote %s (%d cpv/source rows; KIO records with CPV: %d, UZP records with CPV: %d)",
                cpv_path,
                len(cpv_counts),
                cpv_totals["KIO"],
                cpv_totals["UZP"],
            )

            dim_path = output_dir / "cpv_division_by_dimension.csv"
            _write_cpv_division_by_dimension(dim_path, kio_crawl, uzp_crawl, cpv_crawl / "joined.json")
            logger.info("Wrote %s", dim_path)

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


def _aggregate_cpv(path):
    """
    Read the poland_cpv ``joined.json`` and return ``(counts, labels, totals)``.

    ``counts`` is ``Counter[(cpv_code, source)] -> int`` where each (source, record_id) pair
    contributes once per distinct CPV — a ruling that cites three notices sharing a CPV still
    increments that CPV by 1. ``labels`` keeps the human-readable CPV name (last seen wins).
    ``totals`` is the count of distinct (source, record_id) pairs that resolved to at least one
    CPV — the denominator for share-within-source.
    """
    counts: Counter[tuple[str, str]] = Counter()
    labels: dict[str, str] = {}
    seen_per_record: dict[tuple[str, object], set[str]] = {}
    records_with_cpv: set[tuple[str, object]] = set()

    for row in _read_jsonl(path):
        record_key = (row["source"], row["record_id"])
        cpv_codes = row.get("cpv_codes") or []
        if not cpv_codes:
            continue
        records_with_cpv.add(record_key)
        seen = seen_per_record.setdefault(record_key, set())
        for entry in cpv_codes:
            code = entry.get("code")
            if not code:
                continue
            if entry.get("label"):
                labels[code] = entry["label"]
            if code in seen:
                continue
            seen.add(code)
            counts[(code, row["source"])] += 1

    totals = {"KIO": 0, "UZP": 0}
    for source, _ in records_with_cpv:
        totals[source] = totals.get(source, 0) + 1
    return counts, labels, totals


# Dimension matchers for the OPZ-quality cross-tab. Each dimension is a predicate over a
# parsed (article, ust) reference set with an (article, "") any-ust marker. The matchers are
# deliberately non-overlapping: art. 116 belongs only to proportionality, art. 99 ust. 2/4/5/6
# only to competition_neutrality. Clarity and internal_consistency require co-citation with
# art. 135 (questions to bidders) and art. 137 (SWZ modification) respectively, because art. 99
# ust. 1 bundles clarity/completeness/correctness/internal-consistency into one citation key
# (the Polish sentence enumerates "jednoznaczny i wyczerpujący ... dostatecznie dokładnych
# i zrozumiałych ..."). The co-citation refinement trades recall for disambiguation.
DIMENSIONS = (
    "correctness",
    "completeness",
    "clarity",
    "internal_consistency",
    "verifiability",
    "proportionality",
    "competition_neutrality",
    "formal_completeness",
)


def _matches_dimension(subs, dim):
    art99_1 = ("99", "1") in subs
    if dim == "correctness":
        return art99_1 or ("103", "") in subs
    if dim == "completeness":
        return any((a, "") in subs for a in ("134", "281", "282", "104", "105"))
    if dim == "clarity":
        return art99_1 and ("135", "") in subs
    if dim == "internal_consistency":
        return art99_1 and ("137", "") in subs
    if dim == "verifiability":
        return ("241", "") in subs or ("95", "") in subs
    if dim == "proportionality":
        return ("112", "") in subs or ("116", "") in subs
    if dim == "competition_neutrality":
        return any(("99", u) in subs for u in ("2", "4", "5", "6"))
    if dim == "formal_completeness":
        return any((a, "") in subs for a in ("433", "436", "437", "439"))
    raise ValueError(dim)


def _parse_to_subs(texts):
    """
    Return ``{(article, ust)}`` for every reference in ``texts``.

    Each reference also seeds an ``(article, "")`` any-ust marker, so dimension matchers can
    test "any ust of article N" with a single membership check.
    """
    out = set()
    for text in texts:
        if not text:
            continue
        for match in SUBCLAUSE_RE.finditer(text):
            article = match["article"]
            ust = match["ust"] or ""
            out.add((article, ust))
            out.add((article, ""))
    return out


# Custom-development / programming sub-prefixes within div 72 — co-codes that signal the
# tender is custom software development rather than packaged-software procurement. Used to
# narrow the 48_simple subset (which would otherwise include 48xxxxxx + 72-programming
# integration projects). 7226 as a whole is mostly support/install/maintenance; only 72262
# (Software development services) is the custom-dev outlier within that cluster.
HARD_72_PREFIXES = ("7223", "7224", "72211", "72212", "72243", "72262")


def _has_specific_in_div(codes, div):
    return any(c.startswith(div) and c != f"{div}000000" for c in codes)


def _is_48_simple(codes):
    has_hard_72 = any(c.startswith(p) for c in codes for p in HARD_72_PREFIXES)
    return _has_specific_in_div(codes, "48") and not has_hard_72


def _is_72_simple(codes):
    return _has_specific_in_div(codes, "72") and not any(c.startswith("722") for c in codes)


def _is_33_simple(codes):
    return _has_specific_in_div(codes, "33") and not any(c.startswith("331") for c in codes)


SIMPLE_SUBSETS = (
    ("48_simple", "Software, info systems — specific codes, no custom-dev 72 co-code", _is_48_simple),
    ("72_simple", "IT services — non-programming (excl. 722, bare-only)", _is_72_simple),
    ("33_simple", "Medical equipment, pharma — non-equipment (excl. 331, bare-only)", _is_33_simple),
)


# 2-digit CPV division labels. Used as the second column of cpv_division_by_dimension.csv;
# joined.json only stores 8-digit labels, so the division labels are kept here statically.
CPV_DIVISION_LABELS = {
    "03": "Agriculture, farming, fishing, forestry",
    "09": "Petroleum, fuel, electricity",
    "14": "Mining, basic metals",
    "15": "Food, beverages, tobacco",
    "16": "Agricultural machinery",
    "18": "Clothing, footwear, luggage",
    "19": "Leather, textile fabrics",
    "22": "Printed matter, paper",
    "24": "Chemical products",
    "30": "Office, computing machinery & equipment",
    "31": "Electrical machinery & apparatus",
    "32": "Radio, TV, communications",
    "33": "Medical equipment, pharmaceuticals",
    "34": "Transport equipment",
    "35": "Security, fire-fighting equipment",
    "37": "Musical instruments, sport, games",
    "38": "Laboratory, optical, precision instruments",
    "39": "Furniture, household, cleaning products",
    "41": "Collected & purified water",
    "42": "Industrial machinery",
    "43": "Mining/quarrying/construction machinery",
    "44": "Construction structures & materials",
    "45": "Construction works",
    "48": "Software, information systems",
    "50": "Repair & maintenance services",
    "51": "Installation services",
    "55": "Hotel, restaurant, retail services",
    "60": "Transport services",
    "63": "Supporting transport services",
    "64": "Postal & telecom services",
    "65": "Public utilities",
    "66": "Financial & insurance services",
    "70": "Real estate services",
    "71": "Architectural, engineering, technical services",
    "72": "IT services",
    "73": "Research & development",
    "75": "Administration, defence, social security",
    "76": "Oil/gas-related services",
    "77": "Agricultural, horticultural services",
    "79": "Business services (legal, marketing, security…)",
    "80": "Education & training services",
    "85": "Health & social work services",
    "90": "Sewage, refuse, cleaning, environmental",
    "92": "Recreational, cultural, sporting",
    "98": "Other community, social, personal services",
}


def _write_cpv_division_by_dimension(out_path, kio_crawl, uzp_crawl, cpv_joined_path):
    """
    Cross-tab of CPV division by OPZ-quality dimension.

    Builds per-record reference sets from rulings.json / findings.json, joins on
    (source, record_id) with joined.json, and writes one row per 2-digit CPV division plus
    one row per ``SIMPLE_SUBSETS`` entry. A record is counted under every division it touches,
    so a multi-CPV tender contributes to multiple rows.
    """
    record_subs = {}
    if kio_crawl:
        for row in _read_jsonl(kio_crawl / "rulings.json"):
            record_subs[("KIO", row["record_id"])] = _parse_to_subs(row.get("articles") or [])
    if uzp_crawl:
        # UZP record_id in joined.json is the per-finding anchor (poland_cpv spider:
        # ``anchor = row.get("anchor") or f"row-{idx}"``).
        for idx, row in enumerate(_read_jsonl(uzp_crawl / "findings.json")):
            anchor = row.get("anchor") or f"row-{idx}"
            record_subs[("UZP", anchor)] = _parse_to_subs([row.get("violation_text") or ""])

    record_cpvs = {}
    for row in _read_jsonl(cpv_joined_path):
        codes = {entry["code"] for entry in (row.get("cpv_codes") or []) if entry.get("code")}
        if not codes:
            continue
        record_cpvs.setdefault((row["source"], row["record_id"]), set()).update(codes)

    record_dims = {
        key: {dim for dim in DIMENSIONS if _matches_dimension(record_subs.get(key, set()), dim)} for key in record_cpvs
    }

    total_per_dim = dict.fromkeys(DIMENSIONS, 0)
    div_counts = {}
    for key, dims in record_dims.items():
        for dim in dims:
            total_per_dim[dim] += 1
        # Skip divisions for records that don't match any dimension — otherwise an all-zero
        # row would be emitted for every division that happens to have a tagged-but-unmatched
        # record (e.g. div 14 / 41 in the current corpus).
        if not dims:
            continue
        for div in {code[:2] for code in record_cpvs[key]}:
            row = div_counts.setdefault(div, dict.fromkeys(DIMENSIONS, 0))
            for dim in dims:
                row[dim] += 1

    def share(count, total):
        return f"{count / total:.4f}" if total else ""

    fieldnames = ["division", "label", *DIMENSIONS, *(f"{dim}_share" for dim in DIMENSIONS)]
    with out_path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(fieldnames)
        writer.writerow(["_total_with_cpv", ""] + [total_per_dim[dim] for dim in DIMENSIONS] + [""] * len(DIMENSIONS))
        for div, counts in sorted(div_counts.items(), key=lambda kv: (-kv[1]["correctness"], kv[0])):
            writer.writerow(
                [div, CPV_DIVISION_LABELS.get(div, "")]
                + [counts[dim] for dim in DIMENSIONS]
                + [share(counts[dim], total_per_dim[dim]) for dim in DIMENSIONS]
            )
        for bucket, label, predicate in SIMPLE_SUBSETS:
            counts = dict.fromkeys(DIMENSIONS, 0)
            for key, codes in record_cpvs.items():
                if predicate(codes):
                    for dim in record_dims[key]:
                        counts[dim] += 1
            writer.writerow(
                [bucket, label]
                + [counts[dim] for dim in DIMENSIONS]
                + [share(counts[dim], total_per_dim[dim]) for dim in DIMENSIONS]
            )
