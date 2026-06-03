import scrapy

from generic_scrapy.base_spiders.export_file_spider import ExportFileSpider


class UzpKontrole(ExportFileSpider):
    """
    UZP control findings (Informacje o wynikach kontroli Prezesa Urzędu) from gov.pl/web/uzp.

    Walks the 17 violation-category landing pages (cases initiated 2016-07-28 onward) and writes
    one JSONL row per finding with title, violation summary (with inline PZP article references)
    and the PDF attachment URLs (kept for the CPV follow-up, not downloaded here).

    Categories use one of two layouts: newer pages render each finding as a
    ``<details class="accordion-detail">`` accordion; older pages render them as flat ``<li>``
    items inside the ``editor-content`` block. Both are parsed.
    """

    name = "uzp_kontrole"

    start_url = (
        "https://www.gov.pl/web/uzp/informacje-o-wynikach-kontroli-prezesa-urzedu"
        "---postepowania-wszczete-od-28072016-r"
    )

    # ExportFileSpider
    export_outputs = {
        "main": {
            "name": "findings",
            "formats": ["json"],
            "item_filter": None,
        },
    }

    async def start(self):
        yield scrapy.Request(self.start_url, callback=self.parse_index)

    def parse_index(self, response):
        # The landing page links to one child per violation category. Filter to children of the
        # current listing — links to "Repozytorium wiedzy" etc. live in the side nav.
        for link in response.xpath(
            "//main//a[starts-with(@href, '/web/uzp/naruszenia-dotyczace-') "
            "or starts-with(@href, '/web/uzp/inne-naruszenia-')]/@href"
        ).getall():
            yield response.follow(link, callback=self.parse_category)

    def parse_category(self, response):
        category = response.xpath("//main//h1//text()").get() or response.xpath("//main//h2//text()").get()
        category = category.strip() if category else None

        accordions = response.xpath("//details[contains(@class, 'accordion-detail')]")
        if accordions:
            yield from self._parse_accordions(response, category, accordions)
        else:
            yield from self._parse_list(response, category)

    def _parse_accordions(self, response, category, accordions):
        for accordion in accordions:
            title = " ".join(t.strip() for t in accordion.xpath("./summary//text()").getall() if t.strip())
            violation_text = " ".join(
                t.strip()
                for t in accordion.xpath(".//div[contains(@class, 'editor-content')]//text()").getall()
                if t.strip()
            )
            attachments = [
                {
                    "url": response.urljoin(anchor.xpath("./@href").get()),
                    "filename": (anchor.xpath(".//span[contains(@class, 'extension')]/text()").get() or "").strip(),
                }
                for anchor in accordion.xpath(".//a[contains(@class, 'file-download')]")
            ]
            if not (
                yield from self._emit(
                    response, category, title, violation_text, attachments, accordion.xpath("./@id").get()
                )
            ):
                return

    def _parse_list(self, response, category):
        # Older pages render findings as <li> entries inside the editor-content block, with
        # title-bearing <a>s to the PDF on uzp.gov.pl and the violation text following inline.
        for li in response.xpath("//div[contains(@class, 'editor-content')]//li"):
            anchors = li.xpath(".//a")
            # The first anchor wraps the procurement title; remaining anchors are the KIO/KD
            # opinion or supporting docs.
            title = (
                " ".join(t.strip() for t in anchors[0].xpath(".//text()").getall() if t.strip()) if anchors else None
            )
            violation_text = " ".join(t.strip() for t in li.xpath(".//text()").getall() if t.strip())
            attachments = [
                {
                    "url": response.urljoin(a.xpath("./@href").get() or ""),
                    "filename": (a.xpath("./@href").get() or "").rsplit("/", 1)[-1],
                }
                for a in anchors
                if a.xpath("./@href").get()
            ]
            if not (yield from self._emit(response, category, title, violation_text, attachments, None)):
                return

    def _emit(self, response, category, title, violation_text, attachments, anchor):
        self.sample_yielded = getattr(self, "sample_yielded", 0) + 1
        yield {
            "source": "UZP",
            "category": category,
            "title": title or None,
            "violation_text": violation_text or None,
            "attachments": attachments,
            "url": response.url,
            "anchor": anchor,
        }
        return not (self.sample and self.sample_yielded >= self.sample)
