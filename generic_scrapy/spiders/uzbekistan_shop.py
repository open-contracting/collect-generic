from generic_scrapy.spiders.uzbekistan_base_spider import UzbekistanBaseSpider


class UzbekistanShop(UzbekistanBaseSpider):
    name = "uzbekistan_shop"

    # ExportFileSpider
    export_outputs = {
        "main": {
            "name": "uzbekistan_shop",
            "formats": ["csv"],
            "item_filter": None,
        }
    }

    # UzbekistanBaseSpider
    base_url = "https://xarid-api-shop.uzex.uz/Common/GetCompletedDeals"

    # BaseSpider
    default_from_date = "2022-01-01T00:00:00"

    def start_requests(self):
        for national in [1, 0]:
            filters = self.build_filters(
                0,
                self.page_size,
                item={
                    "display_on_shop": 0 if national else 1,
                    "display_on_national": national,
                },
            )
            yield self.build_request(filters, callback=self.parse_list)

    def parse(self, response, **kwargs):
        for item in response.json():
            item["procurement_method"] = "National Store" if item["display_on_national"] else "Electronic Store"
            yield item

    def build_filters(self, from_parameter, to_parameter, **kwargs):
        filters = super().build_filters(from_parameter, to_parameter)
        filters["display_on_shop"] = kwargs["item"]["display_on_shop"]
        filters["display_on_national"] = kwargs["item"]["display_on_national"]
        return filters
