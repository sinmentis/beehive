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

import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from html.parser import HTMLParser

from beehive.connectors.base import CommentFetchTarget, RawItem, as_utc
from beehive.connectors.http import fetch_bytes
from beehive.connectors.registry import register
from beehive.domain.channels import ChannelKind

_BODY_CHAR_CAP = 1500
_FETCH_LIMIT = 50
_ATOM_NS = "{http://www.w3.org/2005/Atom}"
_USER_AGENT = "beehive/0.1 (by /u/sinmentis)"
_LOGGER = logging.getLogger(__name__)
# fetch_comments builds its URL from a stored `<link href>` that arrived inside a feed, so it is
# externally influenced input, not something this module constructed. Constraining it to Reddit
# is both the SSRF control and a correctness one: appending "/.rss" is only meaningful on a
# Reddit permalink in the first place.
_REDDIT_HOSTS = frozenset({"reddit.com"})


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
    return fetch_bytes(url, allowed_hosts=_REDDIT_HOSTS, user_agent=_USER_AGENT).body


def _default_fetch_comment_rss(item_url: str) -> bytes:
    """`item_url` is a stored `<link href>` from a previously fetched feed entry, so it is
    validated (scheme, port, no credentials, Reddit host, globally routable address) before any
    request is made -- an unvalidated urlopen here reached `file://` and loopback URLs."""
    url = f"{item_url.rstrip('/')}/.rss"
    return fetch_bytes(url, allowed_hosts=_REDDIT_HOSTS, user_agent=_USER_AGENT).body


def _to_raw_item(entry) -> RawItem:
    published_el = entry.find(f"{_ATOM_NS}published")
    created_at = None
    if published_el is not None and published_el.text:
        try:
            created_at = as_utc(datetime.fromisoformat(published_el.text.strip()))
        except ValueError:
            created_at = None
    id_el = entry.find(f"{_ATOM_NS}id")
    title_el = entry.find(f"{_ATOM_NS}title")
    link_el = entry.find(f"{_ATOM_NS}link")
    if id_el is None or not id_el.text:
        raise ValueError("Atom entry has no <id>")
    href = link_el.get("href") if link_el is not None else None
    if not href:
        raise ValueError(f"Atom entry {id_el.text!r} has no <link href>")
    return RawItem(
        external_id=id_el.text,
        title=(title_el.text or "") if title_el is not None else "",
        url=href,
        body=_extract_entry_body(entry),
        created_at=created_at,
        raw_metadata={"author": _extract_author(entry)},
    )


_SUBREDDIT_RE = re.compile(r"\A[A-Za-z0-9_]{2,21}\Z")


class RedditSubredditConnector:
    type_key = "reddit_subreddit"
    supported_channel_kinds = frozenset({ChannelKind.EDITORIAL})

    def __init__(self, fetch_rss=_default_fetch_rss, fetch_comment_rss=_default_fetch_comment_rss):
        self._fetch_rss = fetch_rss
        self._fetch_comment_rss = fetch_comment_rss

    def validate_config(self, config: dict) -> None:
        subreddit = config.get("subreddit")
        if not subreddit:
            raise ValueError("reddit_subreddit config needs a non-empty 'subreddit' key")
        # The value is interpolated straight into a URL path, so anything outside Reddit's own
        # subreddit charset is either a typo or an attempt to reach a different endpoint
        # ("r/../../user/x/..."). Rejecting it here means the admin sees the error at save time.
        if not _SUBREDDIT_RE.fullmatch(str(subreddit)):
            raise ValueError(
                "reddit_subreddit 'subreddit' must be 2-21 characters of A-Z, a-z, 0-9 or _"
            )

    def fetch(self, config: dict) -> list[RawItem]:
        subreddit_name = config["subreddit"]
        raw_xml = self._fetch_rss(subreddit_name, _FETCH_LIMIT)
        root = ET.fromstring(raw_xml)  # noqa: S314 (reddit.com-only, size-capped by fetch_bytes)
        items = []
        for entry in root.findall(f"{_ATOM_NS}entry"):
            try:
                items.append(_to_raw_item(entry))
            except ValueError as exc:
                # One malformed entry used to discard the whole cycle's worth of items for this
                # channel. Reddit occasionally serves an entry with no <link href> (removed post).
                _LOGGER.warning("Skipping malformed r/%s entry: %s", subreddit_name, exc)
        return items

    def fetch_comments(self, target: CommentFetchTarget) -> list[str]:
        raw_xml = self._fetch_comment_rss(target.url)
        root = ET.fromstring(raw_xml)  # noqa: S314 (Reddit's own feed, not user input)
        entries = root.findall(f"{_ATOM_NS}entry")
        if len(entries) < 2:
            return []
        comment_text = _extract_entry_body(entries[1])
        return [comment_text] if comment_text else []


register(RedditSubredditConnector())
