import scrapy
from scrapy import Selector
from scrapy.exceptions import UsageError

from generic_scrapy.base_spiders.export_file_spider import ExportFileSpider
from generic_scrapy.filters import PolandContractorFilter, PolandNoticeFilter
from generic_scrapy.util import replace_parameters


class Poland(ExportFileSpider):
    name = "poland"

    base_url = "https://ezamowienia.gov.pl/mo-board/api/v1/notice"

    notice_types = [
        "ContractNotice",
        "TenderResultNotice",
        "CompetitionNotice",
        "NoticeUpdateNotice",
        "AgreementUpdateNotice",
        "ContractPerformingNotice",
        "CircumstancesFulfillmentNotice",
        "SmallContractNotice",
        "ConcessionNotice",
        "NoticeUpdateConcession",
        "ConcessionAgreementNotice",
        "ConcessionUpdateAgreementNotice",
    ]

    # ExportFileSpider
    export_outputs = {
        "main": {
            "name": "poland_notices",
            "formats": ["csv"],
            "item_filter": PolandNoticeFilter,
        },
        "secondary": {
            "name": "poland_contractors",
            "formats": ["csv"],
            "item_filter": PolandContractorFilter,
        },
    }

    # BaseSpider
    date_required = True
    default_from_date = "2021-01-01T00:00:00"

    @classmethod
    def from_crawler(cls, crawler, notice_type=None, *args, **kwargs):
        spider = super().from_crawler(crawler, notice_type=notice_type, *args, **kwargs)
        if notice_type and spider.notice_type not in spider.notice_types:
            raise UsageError(f"spider argument `notice_type`: {spider.system!r} not recognized")
        return spider

    def start_requests(self):
        for notice_type in self.notice_types:
            if self.notice_type and notice_type != self.notice_type:
                continue
            url = (
                f"{self.base_url}?NoticeType={notice_type}&PublicationDateFrom={self.from_date}"
                f"&PublicationDateTo={self.until_date}&PageSize=100"
            )
            yield scrapy.Request(url)

    def parse(self, response, **kwargs):
        last_object_id = None
        for item in response.json():
            html_dict = {}
            field_name = None
            last_object_id = item["objectId"]
            html_part = Selector(text=item["htmlBody"]).css(".mb-0")
            for element in html_part:
                value = element.xpath("text()").get(default="").strip()
                # If the element is a field name
                if "<h3" in element.get():
                    # If the value of the field is inside as a span element
                    html_dict[value] = element.xpath("span/text()").get(default="")
                    # If not, the value is in the subsequents html elements, and we store the field name
                    if not html_dict[value]:
                        field_name = value
                # If the element is not a field name, then it is the value of the previous field name
                else:
                    html_dict[field_name] += value
            full_item = item | html_dict
            if item["contractors"]:
                for contractor in item["contractors"]:
                    if contractor["contractorName"]:
                        contractor["tenderId"] = item["tenderId"]
                        yield contractor
            full_item.pop("htmlBody")
            full_item.pop("contractors")
            yield full_item
        if last_object_id:
            yield scrapy.Request(replace_parameters(response.request.url, SearchAfter=last_object_id))
