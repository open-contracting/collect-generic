import datetime as dt
from pathlib import Path

import scrapy
from scrapy.exceptions import UsageError

VALID_DATE_FORMATS = {
    "date": "%Y-%m-%d",
    "datetime": "%Y-%m-%dT%H:%M:%S",
    "year": "%Y",
    "year-month": "%Y-%m",
}


class BaseSpider(scrapy.Spider):
    """
    The base class for this project's spiders.

    With respect to the data's source:

    -  If the source can support ``from_date`` and ``until_date`` spider arguments:

       -  Set a ``date_format`` class attribute to "date", "datetime", "year" or "year-month" (default "date").
       -  Set a ``default_from_date`` class attribute to a date ("YYYY-MM-DD"), datetime ("YYYY-MM-DDTHH:MM:SS"),
          year ("YYYY") or year-month ("YYYY-MM").

    -  If the spider requires date parameters to be set, add a ``date_required = True`` class attribute, and set the
       ``date_format`` and ``default_from_date`` class attributes as above.

    To store items in ``FILES_STORE/<spider name>/<crawl_directory>/<export_name>.json`` as JSON Lines, set an
    ``export_name`` class attribute.

    .. tip::

        If ``date_required`` is ``True``, or if either the ``from_date`` or ``until_date`` spider arguments are set,
        then ``from_date`` defaults to the ``default_from_date`` class attribute, and ``until_date`` defaults to the
        current time.

    """

    # Regarding the data source.
    date_format = "date"
    date_required = False

    # Regarding the output.
    export_name = None

    def __init__(
        self,
        sample=None,
        from_date=None,
        until_date=None,
        crawl_time=None,
        *args,
        **kwargs,
    ):
        """
        Initialize the spider.

        :param from_date: the date from which to download data (see :ref:`spider-arguments`)
        :param until_date: the date until which to download data (see :ref:`spider-arguments`)
        :param crawl_time: override the crawl's start time, which names the crawl directory
        """
        super().__init__(*args, **kwargs)

        # https://docs.scrapy.org/en/latest/topics/spiders.html#spider-arguments

        # Related to filtering data from the source.
        self.sample = sample
        self.from_date = from_date
        self.until_date = until_date

        self.date_format = VALID_DATE_FORMATS[self.date_format]

        # Related to incremental crawls.
        self.crawl_time = crawl_time

        spider_arguments = {
            "sample": sample,
            "from_date": from_date,
            "until_date": until_date,
            "crawl_time": crawl_time,
        }
        spider_arguments.update(kwargs)

        self.logger.info("Spider arguments: %r", spider_arguments)

    @classmethod
    def update_settings(cls, settings):
        super().update_settings(settings)
        if cls.export_name:
            file_path = Path(settings["FILES_STORE"]) / "%(name)s/%(crawl_directory)s" / f"{cls.export_name}.json"
            settings.set("FEEDS", {str(file_path): {"format": "jsonlines"}}, priority="spider")

    @classmethod
    def from_crawler(cls, crawler, *args, **kwargs):
        spider = super().from_crawler(crawler, *args, **kwargs)

        if spider.sample:
            try:
                spider.sample = int(spider.sample)
            except ValueError:
                raise UsageError(f"spider argument `sample`: invalid integer value: {spider.sample!r}") from None

        if spider.crawl_time:
            try:
                spider.crawl_time = dt.datetime.strptime(spider.crawl_time, "%Y-%m-%dT%H:%M:%S").replace(
                    tzinfo=dt.timezone.utc
                )
            except ValueError as e:
                raise UsageError(f"spider argument `crawl_time`: invalid date value: {e}") from None
        else:
            spider.crawl_time = dt.datetime.now(tz=dt.timezone.utc)
        spider.crawl_directory = spider.crawl_time.strftime("%Y%m%d_%H%M%S")

        if spider.from_date or spider.until_date or spider.date_required:
            if not spider.from_date:
                spider.from_date = spider.default_from_date
            try:
                if isinstance(spider.from_date, str):
                    spider.from_date = spider.parse_date_argument(spider.from_date)
            except ValueError as e:
                raise UsageError(f"spider argument `from_date`: invalid date value: {e}") from None

            if not spider.until_date:
                spider.until_date = dt.datetime.now(tz=dt.timezone.utc)
            try:
                if isinstance(spider.until_date, str):
                    spider.until_date = spider.parse_date_argument(spider.until_date)
            except ValueError as e:
                raise UsageError(f"spider argument `until_date`: invalid date value: {e}") from None

        return spider

    def parse_date_argument(self, date):
        """Return the date argument as a datetime object."""
        return dt.datetime.strptime(date, self.date_format).replace(tzinfo=dt.timezone.utc)
