"""The one human-readable label for a Source, shared by the admin UI, digest warnings, and
collector logs, so an Owner sees the same name for a Source everywhere. Pure helpers: importing
this module never loads the web application."""
from __future__ import annotations

import json
from urllib.parse import urlparse

from beehive.connectors.international_clearance import RETAILER_LABELS
from beehive.localization import Localizer
from beehive.web.hackernews_labels import hackernews_source_label
from beehive.web.official_feed_labels import official_feed_label


def derived_source_label(source: dict, t: Localizer) -> str:
    """A label built from the Source's type and config, ignoring any Owner-supplied name."""
    config = json.loads(source["config"])
    if source["type"] == "reddit_subreddit":
        return f"r/{config['subreddit']}"
    if source["type"] == "google_news_query":
        return f'"{config["query"]}"'
    if source["type"] == "all_about_auctions":
        return "All About Auctions"
    if source["type"] in {"shopify_collection", "land_sea_collection"}:
        # Both connectors store the same {"collection_url": ...} config shape.
        url = config.get("collection_url", "")
        parsed = urlparse(url)
        return f"{parsed.netloc}{parsed.path}" if parsed.netloc else url
    if source["type"] == "international_clearance":
        retailer = config.get("retailer")
        label = RETAILER_LABELS.get(
            retailer,
            str(retailer or source["type"]),
        )
        minimum = config.get("minimum_discount_percent", 70)
        return f"{label} · {minimum}%+"
    official_label = official_feed_label(source["type"])
    if official_label is not None:
        return official_label
    hackernews_label = hackernews_source_label(source["type"], config, t)
    return hackernews_label if hackernews_label is not None else source["type"]


def source_display_name(source: dict, t: Localizer) -> str:
    """The Owner's own name for the Source when set, otherwise the derived label."""
    name = (source.get("name") or "").strip()
    if name:
        return name
    try:
        return derived_source_label(source, t)
    except (KeyError, TypeError, ValueError):
        # A corrupt config must not break an email or a log line that is reporting on it.
        return source["type"]
