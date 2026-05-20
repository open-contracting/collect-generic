class UzbekistanAuctionFilter:
    def __init__(self, feed_options):
        self.feed_options = feed_options

    def accepts(self, item):
        return "product_name" not in item


class UzbekistanAuctionProductFilter:
    def __init__(self, feed_options):
        self.feed_options = feed_options

    def accepts(self, item):
        return "product_name" in item


class UzbekistanDealFilter:
    def __init__(self, feed_options):
        self.feed_options = feed_options

    def accepts(self, item):
        return "deal_id" in item


class UzbekistanDealTradeFilter:
    def __init__(self, feed_options):
        self.feed_options = feed_options

    def accepts(self, item):
        return "deal_id" not in item
