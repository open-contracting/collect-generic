import os

from generic_scrapy.base_spiders.base_spider import BaseSpider
from generic_scrapy.settings import FILES_STORE


class ExportFileSpider(BaseSpider):
    """
    Store items in a CSV file, JSON file or both, using Scrapy's CSV and JSON Item Exporters.

    #. Inherit from ``ExportFileSpider``
    #. Define a ``export_outputs`` dict, with the following structure:
        ```
        {
            'main': {
                'name': 'main_file_name',
                'formats': ['json', 'csv'],
                'item_filter': generic_scrapy.filters.MyCustomFilter2',
            },
            'secondary': {
                'name': 'extra_file_name"',
                'formats': ['json', 'csv'],
                'item_filter': generic_scrapy.filters.MyCustomFilter2',
                'overwrite': True
                }
            }
        }
        ```
        Where the 'main' key contains the main table to generate from the spider output and 'secondary' is for any
        additional table the spider might generate.
        The files will be stored under settings.FILES_STORE/spider.name/spider.crawl_directory

    """

    files_store = None
    export_outputs = {}

    @classmethod
    def update_settings(cls, settings):
        super().update_settings(settings)
        feeds = {}
        for entry in cls.export_outputs:
            item_filter = cls.export_outputs[entry]["item_filter"]
            file_name = cls.export_outputs[entry]["name"]
            for export_format in cls.export_outputs[entry]["formats"]:
                file_path = os.path.join(
                    settings.get("FILES_STORE"),
                    "%(name)s/%(crawl_directory)s",
                    f"{file_name}.{export_format}",
                )
                feeds[file_path] = {"format": "jsonlines" if export_format == "json" else "csv"}
                if item_filter:
                    feeds[file_path]["item_filter"] = item_filter
                if "overwrite" in cls.export_outputs[entry]:
                    feeds[file_path]["overwrite"] = cls.export_outputs[entry]["overwrite"]
        settings.set("FEEDS", feeds, priority="spider")

    @classmethod
    def get_file_store_directory(cls):
        return os.path.join(FILES_STORE, cls.name, cls.crawl_directory)
