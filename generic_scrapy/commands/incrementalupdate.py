import csv
import os.path
from datetime import datetime, timedelta

from scrapy.commands import ScrapyCommand
from scrapy.exceptions import UsageError

from generic_scrapy.base_spiders.base_spider import VALID_DATE_FORMATS


class IncrementalUpdate(ScrapyCommand):
    def short_desc(self):
        return (
            "Given a spider name, crawl_directory and the field name to check for the dataset latest date, gets new "
            "data from the latest date, updating the existing files in crawl_directory. Only works for "
            "spiders that inherit from ExportFileSpider with CSV as the export format."
        )

    def syntax(self):
        return "[options] [spider]"

    def add_options(self, parser):
        ScrapyCommand.add_options(self, parser)
        parser.add_argument(
            "--date_field_name",
            type=str,
            help="The date field to use for checking for the number of items downloaded the last time.",
        )
        parser.add_argument(
            "--crawl_directory",
            type=str,
            help="The crawl_directory where previous data was stored",
        )

    def run(self, args, opts):
        if not args:
            raise UsageError("A spider name must be set.")

        spider_name = args[0]
        if spider_name not in self.crawler_process.spider_loader.list():
            raise UsageError("The spider does not exist")

        spidercls = self.crawler_process.spider_loader.load(spider_name)

        if not hasattr(spidercls, "export_outputs"):
            raise UsageError("The selected spider must be a ExportFileSpider")

        if opts.crawl_directory:
            spidercls.crawl_directory = opts.crawl_directory

        max_date = None
        if opts.date_field_name:
            with open(
                os.path.join(spidercls.get_file_store_directory(), f"{spidercls.export_outputs['main']['name']}.csv")
            ) as f:
                max_date = datetime.strptime(
                    max(row[opts.date_field_name] for row in csv.DictReader(f)), VALID_DATE_FORMATS["datetime"]
                ).replace(tzinfo=datetime.timezone.utc) + timedelta(seconds=1)

        self.crawler_process.crawl(spidercls, from_date=max_date, crawl_directory=opts.crawl_directory)
        self.crawler_process.start()
