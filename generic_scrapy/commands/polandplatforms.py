__review_status__ = "unreviewed"
__generated_by__ = "claude-code/claude-opus-5-5"
__generated_at__ = "2026-11-24"

import collections
import csv
import json
import sys
from pathlib import Path

from scrapy.commands import ScrapyCommand
from scrapy.exceptions import UsageError

from generic_scrapy.poland.common import host, require_store
from generic_scrapy.poland.eb2b import EB2B_HOSTS
from generic_scrapy.poland.josephine import JOSEPHINE_HOST
from generic_scrapy.poland.marketplanet import MARKETPLANET_HOSTS
from generic_scrapy.poland.smartpzp import SMARTPZP_SLOT
from generic_scrapy.poland.zeto import ZETO_HOSTS

STORES = ["poland_tender_documents", "poland_kio_tender_documents"]


def adapter(name):
    """Return the poland spider's adapter for a host, as in ``Poland.collect_control``, or an empty string."""
    if name == "ezamowienia.gov.pl":
        return "ezamowienia"
    if name.endswith(MARKETPLANET_HOSTS):
        return "marketplanet"
    if name.endswith("logintrade.net"):
        return "logintrade"
    if name.endswith(EB2B_HOSTS):
        return "eb2b"
    if name == SMARTPZP_SLOT:
        return "smartpzp"
    if name in ZETO_HOSTS:
        return "zeto"
    if name == JOSEPHINE_HOST:
        return "josephine"
    return ""


def row_host(row):
    if row["source"] == "ezamowienia":
        return "ezamowienia.gov.pl"
    if row["source"].startswith("platform:"):
        return host(row["source"].removeprefix("platform:"))
    return host(row["procedure_url"]) if row.get("procedure_url") else row["source"]


class PolandPlatforms(ScrapyCommand):
    def short_desc(self):
        return (
            "Count the poland spider's controls by platform: collected by hand, crawled and (with --tasks) open in "
            "the hand collection tasks, with the spider's adapter for each, to find platforms worth an adapter."
        )

    def syntax(self):
        return "[options]"

    def add_options(self, parser):
        ScrapyCommand.add_options(self, parser)
        parser.add_argument(
            "--stores",
            default=",".join(STORES),
            help=f"Comma-separated stores under FILES_STORE (default: {','.join(STORES)})",
        )
        parser.add_argument("--tasks", help="A hand collection CSV with control_id and procedure_url columns")
        parser.add_argument("--output", help="Write a CSV here instead of printing a table")

    def run(self, args, opts):
        if args:
            raise UsageError
        counts = collections.defaultdict(collections.Counter)
        for store in opts.stores.split(","):
            directory = require_store(Path(self.settings["FILES_STORE"], store))
            with (directory / "manifest.json").open(encoding="utf-8") as f:
                for row in map(json.loads, f):
                    counts[row_host(row)][row["collection"]] += 1
        if opts.tasks:
            with Path(opts.tasks).open(encoding="utf-8", newline="") as f:
                controls = {
                    (r["control_id"], host(r["procedure_url"]) if r["procedure_url"] else "(none)")
                    for r in csv.DictReader(f)
                }
            for _, name in controls:
                counts[name]["tasks"] += 1

        header = ["host", "adapter", "manual", "crawl", "tasks"]
        rows = [
            [name, adapter(name), c["manual"], c["crawl"], c["tasks"]]
            for name, c in sorted(counts.items(), key=lambda item: (-item[1]["manual"], -item[1]["tasks"], item[0]))
        ]
        if opts.output:
            with Path(opts.output).open("w", encoding="utf-8", newline="") as f:
                csv.writer(f).writerows([header, *rows])
            return
        width = max(len(str(r[0])) for r in rows)
        sys.stdout.write(f"{'host':<{width}}  {'adapter':<12}  manual  crawl  tasks\n")
        for name, kind, manual, crawl, tasks in rows:
            sys.stdout.write(f"{name:<{width}}  {kind:<12}  {manual:>6}  {crawl:>5}  {tasks:>5}\n")
