"""The one human-readable label for a Source, used by the reading pages, the admin UI, digest
emails, research views and collector logs, so an Owner sees the same name for a Source everywhere.

Pure helpers below the web layer: channels/views.py, collector/ and digest/ import this module
directly, and importing it never loads the web application.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from urllib.parse import urlparse

from beehive.connectors.international_clearance import RETAILER_LABELS
from beehive.localization import Localizer

OFFICIAL_FEED_LABELS = {
    "rbnz_news": "RBNZ News",
    "nz_government_news": "NZ Government",
    "federal_reserve_news": "Federal Reserve",
}

HN_FEED_KEYS = {
    "top": "web.hn.feed.top",
    "best": "web.hn.feed.best",
    "new": "web.hn.feed.new",
    "ask": "web.hn.feed.ask",
    "show": "web.hn.feed.show",
    "job": "web.hn.feed.job",
}


def official_feed_label(source_type: str) -> str | None:
    return OFFICIAL_FEED_LABELS.get(source_type)


def hackernews_source_label(
    source_type: str, config: Mapping[str, object], t: Localizer
) -> str | None:
    """The label for a Hacker News Source, or None for any other type. An unknown configured feed
    shows its raw value after the "HN · " prefix instead of failing."""
    if source_type == "hackernews_stories":
        feed = config.get("feed", "")
        feed = feed if isinstance(feed, str) else ""
        feed_key = HN_FEED_KEYS.get(feed)
        feed_label = t.text(feed_key) if feed_key is not None else feed
        return t.text("web.hn.stories_label", feed=feed_label)
    if source_type == "hackernews_query":
        return t.text("web.hn.query_label", query=config.get("query", ""))
    return None


def collection_host_label(config: Mapping[str, object]) -> str:
    """A storefront collection as host plus path, e.g. "shop.example.com/collections/sale"."""
    url = config.get("collection_url")
    url = url if isinstance(url, str) else ""
    parsed = urlparse(url)
    return f"{parsed.netloc}{parsed.path}" if parsed.netloc else url


def source_label(source_type: str, config: Mapping[str, object], t: Localizer) -> str:
    """The reader-facing label built from a Source's type and config. Falls back to the type
    itself for anything it does not recognise, never an error."""
    if source_type == "reddit_subreddit":
        return f"r/{config.get('subreddit', '')}"
    if source_type == "google_news_query":
        return f'"{config.get("query", "")}"'
    if source_type == "all_about_auctions":
        return "All About Auctions"
    if source_type in {"shopify_collection", "land_sea_collection"}:
        return collection_host_label(config)
    if source_type == "international_clearance":
        retailer = config.get("retailer")
        return RETAILER_LABELS.get(retailer, str(retailer or source_type))
    official = official_feed_label(source_type)
    if official is not None:
        return official
    hackernews = hackernews_source_label(source_type, config, t)
    return hackernews if hackernews is not None else source_type


def parse_source_config(raw: object) -> Mapping[str, object]:
    """A Source's stored JSON config as a mapping; anything unreadable becomes an empty one."""
    if not isinstance(raw, str):
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def derived_source_label(source: Mapping[str, object], t: Localizer) -> str:
    """The Owner-facing label for a `sources` row, ignoring any Owner-supplied name. Same as
    `source_label`, plus the discount threshold for clearance sources so two thresholds on one
    retailer stay distinguishable in the admin list."""
    source_type = str(source["type"])
    config = parse_source_config(source.get("config"))
    label = source_label(source_type, config, t)
    if source_type == "international_clearance":
        minimum = config.get("minimum_discount_percent", 70)
        return f"{label} · {minimum}%+"
    return label


def source_display_name(source: Mapping[str, object], t: Localizer) -> str:
    """The Owner's own name for the Source when set, otherwise the derived label."""
    name = str(source.get("name") or "").strip()
    return name or derived_source_label(source, t)
