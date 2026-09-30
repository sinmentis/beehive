"""Plain text from the HTML fragments feeds put in titles and descriptions."""
from __future__ import annotations

from html.parser import HTMLParser


class _PlainTextExtractor(HTMLParser):
    _BLOCK_TAGS = {"p", "li", "blockquote", "pre", "br"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data):
        self._parts.append(data)

    def text(self) -> str:
        raw = "".join(self._parts)
        lines = (" ".join(line.split()) for line in raw.splitlines())
        return "\n".join(line for line in lines if line)


def html_to_text(value: object, *, cap: int) -> str:
    """The readable text of an HTML fragment, one line per block, cut to `cap` characters.
    Anything that is not a non-empty string gives ""."""
    if not isinstance(value, str) or not value:
        return ""
    extractor = _PlainTextExtractor()
    extractor.feed(value)
    extractor.close()
    return extractor.text()[:cap]


def single_line(value: object, *, cap: int) -> str:
    """Like html_to_text, but folded onto one line, for titles and names."""
    return " ".join(html_to_text(value, cap=cap * 4).split())[:cap]
