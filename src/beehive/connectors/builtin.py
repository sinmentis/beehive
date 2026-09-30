"""Importing this module registers every built-in connector. The admin, the collector and the
Source/Channel policy import it once instead of each listing the connector modules."""
from beehive.connectors import (  # noqa: F401 (import side effect: registers the connectors)
    all_about_auctions,
    google_news,
    hackernews,
    international_clearance,
    land_sea_collection,
    official_feeds,
    reddit,
    rss_feed,
    shopify_collection,
)
