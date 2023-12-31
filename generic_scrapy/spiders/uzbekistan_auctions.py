import scrapy

from generic_scrapy.filters import UzbekistanAuctionFilter, UzbekistanAuctionProductFilter
from generic_scrapy.spiders.uzbekistan_base_spider import UzbekistanBaseSpider


class UzbekistanAuctions(UzbekistanBaseSpider):
    name = "uzbekistan_auctions"

    # ExportFileSpider
    export_outputs = {
        "main": {
            "name": "uzbekistan_auction",
            "formats": ["csv"],
            "item_filter": UzbekistanAuctionFilter,
        },
        "secondary": {
            "name": "uzbekistan_auction_item",
            "formats": ["csv"],
            "item_filter": UzbekistanAuctionProductFilter,
        },
    }

    # UzbekistanBaseSpider
    base_url = "https://xarid-api-auction.uzex.uz/Common/GetCompletedDeals"
    parse_callback = "parse_auctions"

    # BaseSpider
    default_from_date = "2022-01-01T00:00:00"

    def parse_auctions(self, response, **kwargs):
        for item in response.json():
            item["procurement_method"] = "Auction"
            yield item
            yield scrapy.Request(
                f"https://xarid-api-auction.uzex.uz/Common/GetCompletedDealProducts/{item['lot_id']}",
                callback=self.parse_auction_product,
                meta={"lot_id": item["lot_id"]},
            )

    def parse_auction_product(self, response, **kwargs):
        for item in response.json():
            item["lot_id"] = response.request.meta["lot_id"]
            yield item
