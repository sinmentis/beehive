"""Phase 1's only concrete SourceConnector. Fetches via Reddit's public, unauthenticated
Atom RSS feed (https://www.reddit.com/r/<subreddit>/hot/.rss) rather than the official OAuth
Data API: Reddit's November 2025 "Responsible Builder Policy" gates all NEW OAuth app
creation behind a manual review process with no fixed timeline or guarantee of approval. The
RSS endpoint remains open to any
client sending a descriptive User-Agent and respecting its rate limit, at the cost of two
fields the feed doesn't carry: `score` and `num_comments` are simply absent from
raw_metadata; collector/run_cycle.py already defaults them to 0 via `.get(key, 0)`, so the AI
ranking prompt's "community engagement" prior just reads 0 for every item instead of
crashing. Tests inject a fake fetch_rss and never touch the network."""
from __future__ import annotations

import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from html.parser import HTMLParser

from beehive.connectors.base import CommentFetchTarget, RawItem
from beehive.connectors.registry import register

_BODY_CHAR_CAP = 1500
_FETCH_LIMIT = 50
_ATOM_NS = "{http://www.w3.org/2005/Atom}"
_USER_AGENT = "beehive/0.1 (by /u/sinmentis)"


class _MarkdownBodyExtractor(HTMLParser):
    """Reddit's RSS <content> is an HTML fragment. A self-text post wraps its body in
    exactly one <div class="md">...</div>; link/image-only posts have no such div at all.
    Extracts just that div's text (joining block-level elements with a newline), discarding
    the thumbnail table and the trailing "submitted by ... [link] [comments]" boilerplate
    that follows it either way."""

    _BLOCK_TAGS = {"p", "li", "blockquote"}

    def __init__(self):
        super().__init__()
        self._depth = 0  # 0 = outside div.md; >=1 = nesting depth inside it
        self._parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "div":
            if self._depth == 0 and dict(attrs).get("class") == "md":
                self._depth = 1
                return
            if self._depth:
                self._depth += 1
        elif tag in self._BLOCK_TAGS and self._depth:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "div" and self._depth:
            self._depth -= 1

    def handle_data(self, data):
        if self._depth:
            self._parts.append(data)

    def text(self) -> str:
        # Each block tag inserted a bare "\n" marker; everything between two markers is one
        # paragraph's raw text, still carrying the source HTML's own irregular whitespace.
        # Collapse that internal whitespace to single spaces and drop empty paragraphs
        # (e.g. the marker inserted by a nested <li> with no text of its own).
        raw = "".join(self._parts)
        paragraphs = (" ".join(line.split()) for line in raw.split("\n"))
        return "\n".join(p for p in paragraphs if p)


def _extract_body(content_html: str) -> str:
    extractor = _MarkdownBodyExtractor()
    extractor.feed(content_html)
    return extractor.text()


def _extract_entry_body(entry) -> str:
    content_el = entry.find(f"{_ATOM_NS}content")
    content_html = (content_el.text or "") if content_el is not None else ""
    return _extract_body(content_html)[:_BODY_CHAR_CAP]


def _extract_author(entry) -> str:
    name_el = entry.find(f"{_ATOM_NS}author/{_ATOM_NS}name")
    if name_el is None or not name_el.text:
        return "[deleted]"
    name = name_el.text.strip()
    return name[len("/u/"):] if name.startswith("/u/") else name


def _default_fetch_rss(subreddit: str, limit: int) -> bytes:
    url = f"https://www.reddit.com/r/{subreddit}/hot/.rss?limit={limit}"
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 (https only)
        return response.read()


def _default_fetch_comment_rss(item_url: str) -> bytes:
    url = f"{item_url.rstrip('/')}/.rss"
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 (https only)
        return response.read()


def _to_raw_item(entry) -> RawItem:
    published_el = entry.find(f"{_ATOM_NS}published")
    created_at = datetime.fromisoformat(published_el.text) if published_el is not None else None
    return RawItem(
        external_id=entry.find(f"{_ATOM_NS}id").text,
        title=entry.find(f"{_ATOM_NS}title").text,
        url=entry.find(f"{_ATOM_NS}link").get("href"),
        body=_extract_entry_body(entry),
        created_at=created_at,
        raw_metadata={"author": _extract_author(entry)},
    )


class RedditSubredditConnector:
    type_key = "reddit_subreddit"

    def __init__(self, fetch_rss=_default_fetch_rss, fetch_comment_rss=_default_fetch_comment_rss):
        self._fetch_rss = fetch_rss
        self._fetch_comment_rss = fetch_comment_rss

    def validate_config(self, config: dict) -> None:
        if not config.get("subreddit"):
            raise ValueError("reddit_subreddit config needs a non-empty 'subreddit' key")

    def fetch(self, config: dict) -> list[RawItem]:
        subreddit_name = config["subreddit"]
        raw_xml = self._fetch_rss(subreddit_name, _FETCH_LIMIT)
        root = ET.fromstring(raw_xml)  # noqa: S314 (Reddit's own feed, not user input)
        entries = root.findall(f"{_ATOM_NS}entry")
        return [_to_raw_item(entry) for entry in entries]

    def fetch_comments(self, target: CommentFetchTarget) -> list[str]:
        raw_xml = self._fetch_comment_rss(target.url)
        root = ET.fromstring(raw_xml)  # noqa: S314 (Reddit's own feed, not user input)
        entries = root.findall(f"{_ATOM_NS}entry")
        if len(entries) < 2:
            return []
        comment_text = _extract_entry_body(entries[1])
        return [comment_text] if comment_text else []


register(RedditSubredditConnector())
