"""Free, credentialless Hacker News Sources.

Official Firebase list endpoints provide exact top/best/new/ask/show/job semantics.
Algolia Search, added below, provides keyword search and one-request discussion trees.
Tests inject fetch_json and never use the network.
"""
from __future__ import annotations

import json
import urllib.request
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError, as_completed
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.parse import urlencode, urlparse

from beehive.connectors.base import CommentFetchTarget, RawItem
from beehive.connectors.registry import register

_OFFICIAL_BASE = "https://hacker-news.firebaseio.com/v0"
_ALGOLIA_BASE = "https://hn.algolia.com/api/v1"
_USER_AGENT = "beehive/0.1 (personal information hub)"
_REQUEST_TIMEOUT_SECONDS = 15
_DETAIL_BATCH_TIMEOUT_SECONDS = 35
_DETAIL_WORKERS = 8
_STORY_FETCH_LIMIT = 30
_BODY_CHAR_CAP = 1500

_QUERY_FETCH_LIMIT = 50
_QUERY_ENDPOINTS = {
    "relevance": "search",
    "recent": "search_by_date",
}

_FEED_ENDPOINTS = {
    "top": "topstories",
    "best": "beststories",
    "new": "newstories",
    "ask": "askstories",
    "show": "showstories",
    "job": "jobstories",
}

JsonFetcher = Callable[[str], Any]


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


def _html_to_text(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return ""
    extractor = _PlainTextExtractor()
    extractor.feed(value)
    return extractor.text()[:_BODY_CHAR_CAP]


def _default_fetch_json(url: str) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(  # noqa: S310 (module constructs fixed HTTPS provider URLs)
        request,
        timeout=_REQUEST_TIMEOUT_SECONDS,
    ) as response:
        payload = response.read()
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Hacker News returned invalid JSON from {url}") from exc


def _discussion_url(item_id: int) -> str:
    return f"https://news.ycombinator.com/item?id={item_id}"


def _usable_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlparse(value)
    return value if parsed.scheme in {"http", "https"} and parsed.netloc else None


def _int_or_zero(value: Any) -> int:
    # Both providers document these fields as non-negative integers. Do not truncate floats.
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _metadata(
    *,
    item_id: int,
    item_type: str,
    author: Any,
    score: Any,
    num_comments: Any,
) -> dict[str, Any]:
    return {
        "provider": "hackernews",
        "hn_id": item_id,
        "hn_url": _discussion_url(item_id),
        "item_type": item_type,
        "author": author if isinstance(author, str) else "",
        "score": _int_or_zero(score),
        "num_comments": _int_or_zero(num_comments),
    }


class _HackerNewsConnector:
    def __init__(self, fetch_json: JsonFetcher = _default_fetch_json):
        self._fetch_json = fetch_json

    def fetch_comments(self, target: CommentFetchTarget) -> list[str]:
        hn_id = target.raw_metadata.get("hn_id")
        if isinstance(hn_id, str) and hn_id.isdigit():
            hn_id = int(hn_id)
        if isinstance(hn_id, bool) or not isinstance(hn_id, int):
            raise ValueError("Hacker News comment target needs integer 'hn_id' metadata")

        payload = self._fetch_json(f"{_ALGOLIA_BASE}/items/{hn_id}")
        if not isinstance(payload, dict) or not isinstance(payload.get("children"), list):
            raise ValueError("Algolia Item response needs a 'children' list")

        for child in payload["children"]:
            if not isinstance(child, dict) or child.get("deleted"):
                continue
            text = _html_to_text(child.get("text"))
            if text:
                return [text]
        return []


def _official_item_to_raw_item(payload: Any) -> RawItem | None:
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise ValueError("official Item payload must be an object")
    if payload.get("deleted") or payload.get("dead"):
        return None

    item_id = payload.get("id")
    title = payload.get("title")
    created_at = payload.get("time")
    item_type = payload.get("type")
    if isinstance(item_id, bool) or not isinstance(item_id, int):
        raise ValueError("official Item needs an integer id")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("official Item needs a non-empty title")
    if isinstance(created_at, bool) or not isinstance(created_at, int):
        raise ValueError("official Item needs an integer time")
    if item_type not in {"story", "job"}:
        raise ValueError(f"unsupported official Item type: {item_type!r}")

    discussion_url = _discussion_url(item_id)
    return RawItem(
        external_id=str(item_id),
        title=title.strip(),
        url=_usable_url(payload.get("url")) or discussion_url,
        body=_html_to_text(payload.get("text")),
        created_at=datetime.fromtimestamp(created_at, tz=timezone.utc),
        raw_metadata=_metadata(
            item_id=item_id,
            item_type=item_type,
            author=payload.get("by"),
            score=payload.get("score"),
            num_comments=payload.get("descendants"),
        ),
    )


class HackerNewsStoriesConnector(_HackerNewsConnector):
    type_key = "hackernews_stories"

    def validate_config(self, config: dict) -> None:
        feed = config.get("feed")
        if feed not in _FEED_ENDPOINTS:
            allowed = ", ".join(sorted(_FEED_ENDPOINTS))
            raise ValueError(
                f"hackernews_stories config needs 'feed' to be one of: {allowed}"
            )

    def fetch(self, config: dict) -> list[RawItem]:
        self.validate_config(config)
        endpoint = _FEED_ENDPOINTS[config["feed"]]
        feed_payload = self._fetch_json(f"{_OFFICIAL_BASE}/{endpoint}.json")
        if not isinstance(feed_payload, list) or not feed_payload:
            raise ValueError("Hacker News feed payload must be a non-empty list of Item IDs")

        item_ids = feed_payload[:_STORY_FETCH_LIMIT]
        if any(
            isinstance(item_id, bool) or not isinstance(item_id, int)
            for item_id in item_ids
        ):
            raise ValueError("Hacker News feed payload contains a non-integer Item ID")

        executor = ThreadPoolExecutor(max_workers=_DETAIL_WORKERS)
        future_to_item = {
            executor.submit(
                self._fetch_json,
                f"{_OFFICIAL_BASE}/item/{item_id}.json",
            ): (position, item_id)
            for position, item_id in enumerate(item_ids)
        }
        parsed: list[tuple[int, RawItem]] = []
        try:
            try:
                completed = as_completed(
                    future_to_item,
                    timeout=_DETAIL_BATCH_TIMEOUT_SECONDS,
                )
                for future in completed:
                    position, item_id = future_to_item[future]
                    try:
                        raw_item = _official_item_to_raw_item(future.result())
                        if raw_item is None:
                            raise ValueError("Item is null, deleted, or dead")
                    except Exception as exc:
                        print(f"[hackernews] skipping official detail item={item_id}: {exc}")
                        continue
                    parsed.append((position, raw_item))
            except FutureTimeoutError:
                for future, (_, item_id) in future_to_item.items():
                    if not future.done():
                        print(
                            "[hackernews] skipping official detail "
                            f"item={item_id}: batch deadline exceeded"
                        )
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        if not parsed:
            raise RuntimeError("Hacker News feed returned no usable Items")
        parsed.sort(key=lambda result: result[0])
        return [raw_item for _, raw_item in parsed]


def _algolia_hit_to_raw_item(payload: Any) -> RawItem:
    if not isinstance(payload, dict):
        raise ValueError("Algolia hit must be an object")
    external_id = payload.get("objectID")
    title = payload.get("title")
    created_at = payload.get("created_at_i")
    if not isinstance(external_id, str) or not external_id.isdigit():
        raise ValueError("Algolia hit needs a numeric objectID")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("Algolia hit needs a non-empty title")
    if isinstance(created_at, bool) or not isinstance(created_at, int):
        raise ValueError("Algolia hit needs an integer created_at_i")

    item_id = int(external_id)
    return RawItem(
        external_id=external_id,
        title=title.strip(),
        url=_usable_url(payload.get("url")) or _discussion_url(item_id),
        body=_html_to_text(payload.get("story_text")),
        created_at=datetime.fromtimestamp(created_at, tz=timezone.utc),
        raw_metadata=_metadata(
            item_id=item_id,
            item_type="story",
            author=payload.get("author"),
            score=payload.get("points"),
            num_comments=payload.get("num_comments"),
        ),
    )


class HackerNewsQueryConnector(_HackerNewsConnector):
    type_key = "hackernews_query"

    def validate_config(self, config: dict) -> None:
        query = config.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("hackernews_query config needs a non-empty 'query' key")
        if config.get("sort") not in _QUERY_ENDPOINTS:
            allowed = ", ".join(sorted(_QUERY_ENDPOINTS))
            raise ValueError(
                f"hackernews_query config needs 'sort' to be one of: {allowed}"
            )

    def fetch(self, config: dict) -> list[RawItem]:
        self.validate_config(config)
        endpoint = _QUERY_ENDPOINTS[config["sort"]]
        params = urlencode(
            {
                "query": config["query"],
                "tags": "story",
                "hitsPerPage": _QUERY_FETCH_LIMIT,
            }
        )
        payload = self._fetch_json(f"{_ALGOLIA_BASE}/{endpoint}?{params}")
        if not isinstance(payload, dict) or not isinstance(payload.get("hits"), list):
            raise ValueError("Algolia response needs a 'hits' list")

        hits = payload["hits"]
        items = []
        for index, hit in enumerate(hits):
            try:
                items.append(_algolia_hit_to_raw_item(hit))
            except Exception as exc:
                print(f"[hackernews] skipping Algolia hit index={index}: {exc}")
        if hits and not items:
            raise RuntimeError("Algolia search returned no usable Items")
        return items


register(HackerNewsStoriesConnector())
register(HackerNewsQueryConnector())
