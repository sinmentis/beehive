"""Single source of truth for the localized Hacker News feed labels shown by both the public
pages (public.py) and the admin Source list (admin.py). Keeping the mapping here — rather than
duplicating it in each layer or importing one route module from the other — means the "HN ·
Top" labels the two layers render can never drift apart, in every supported platform language."""
from __future__ import annotations

from beehive.localization import Localizer

HN_FEED_KEYS = {
    "top": "web.hn.feed.top",
    "best": "web.hn.feed.best",
    "new": "web.hn.feed.new",
    "ask": "web.hn.feed.ask",
    "show": "web.hn.feed.show",
    "job": "web.hn.feed.job",
}


def hackernews_source_label(source_type: str, config: dict, t: Localizer) -> str | None:
    """Return the display label for a Hacker News Source, or None for non-HN types. An
    unrecognized configured feed falls back to the raw feed value after the "HN · " prefix
    rather than crashing or exposing the source type."""
    if source_type == "hackernews_stories":
        feed = config.get("feed", "")
        feed_key = HN_FEED_KEYS.get(feed)
        feed_label = t.text(feed_key) if feed_key is not None else feed
        return t.text("web.hn.stories_label", feed=feed_label)
    if source_type == "hackernews_query":
        return t.text("web.hn.query_label", query=config.get("query", ""))
    return None
