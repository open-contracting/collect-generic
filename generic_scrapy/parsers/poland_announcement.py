import re

from scrapy import Selector

# Matches an h3 prefix like "1.5.1.) Ulica" or "1.4) Krajowy Numer Identyfikacyjny: REGON 180792174".
# Group 1: dotted key (e.g. "1.5.1"). Group 2: label. Group 3 (optional): post-colon remainder.
KEY_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)*)\.?\)\s*(.*?)(?::\s*(.*))?$", re.DOTALL)


def _norm(text):
    return " ".join((text or "").split())


def _text(selector):
    return _norm(" ".join(selector.xpath(".//text()").getall()))


def parse_announcement(html):
    """
    Parse a Polish BZP announcement (mo-board GetNoticeHtmlBody) into a structured dict.

    Returns ``{"title": str|None, "sections": [...], "_unparsed": [...]}`` where each section
    is ``{"title": str, "items": [{"key", "label", "value", "extra"}]}``.
    """
    sel = Selector(text=html)

    title = _text(sel.xpath("//h1[1]")) or None

    sections = []
    current = None
    unparsed = []

    for node in sel.xpath("//h2 | //h3 | //p"):
        tag = node.root.tag
        if tag == "h2":
            if current is not None:
                sections.append(current)
            current = {"title": _text(node), "items": []}
        elif tag == "h3":
            full_text = _text(node)
            span_text = _text(node.xpath("span[contains(@class, 'normal')]")) or None
            match = KEY_RE.match(full_text)
            if not match:
                unparsed.append({"section": current["title"] if current else None, "text": full_text})
                continue
            key, label, after_colon = match.group(1), _norm(match.group(2)), _norm(match.group(3) or "") or None
            value = span_text or after_colon
            item = {"key": key, "label": label, "value": value, "extra": None}
            (current["items"] if current is not None else unparsed).append(item)
        elif tag == "p":
            text = _text(node)
            if not text or not current or not current["items"]:
                continue
            last = current["items"][-1]
            last["extra"] = f"{last['extra']} {text}" if last["extra"] else text

    if current is not None:
        sections.append(current)

    return {"title": title, "sections": sections, "_unparsed": unparsed}
