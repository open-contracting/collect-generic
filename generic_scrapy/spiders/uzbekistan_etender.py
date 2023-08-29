import scrapy

from generic_scrapy.filters import UzbekistanDealFilter, UzbekistanDealTradeFilter
from generic_scrapy.spiders.uzbekistan_base_spider import UzbekistanBaseSpider


class UzbekistanEtender(UzbekistanBaseSpider):
    name = "uzbekistan_etender"

    # ExportFileSpider
    export_outputs = {
        "main": {
            "name": "uzbekistan_etender",
            "formats": ["csv"],
            "item_filter": UzbekistanDealFilter,
        },
        "secondary": {
            "name": "uzbekistan_etender_trades",
            "formats": ["csv"],
            "item_filter": UzbekistanDealTradeFilter,
        },
    }

    # UzbekistanBaseSpider
    base_url = "https://apietender.uzex.uz/api/common/DealsList"
    parse_callback = "parse_deals"

    # BaseSpider
    default_from_date = "2021-01-01T00:00:00"

    def parse_deals(self, response, **kwargs):
        for item in response.json():
            yield item
            yield scrapy.Request(
                f"https://apietender.uzex.uz/api/common/GetTrade/{item['trade_id']}",
                callback=self.parse_trade,
            )

    def parse_trade(self, response, **kwargs):
        data = response.json()
        data["procurement_method"] = 'Electronic tender' if data["type_name"] == 'Тендер' else 'Electronic competition'
        yield data

    def build_filters(self, from_parameter, to_parameter, **kwargs):
        filters = {
            "From": from_parameter,
            "To": to_parameter,
            "DeadlineStart": self.from_date.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        }
        if self.until_date:
            filters["DeadlineEnd"] = self.until_date.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        return filters
