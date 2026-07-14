"""Single source of truth for the Chinese Hacker News feed labels shown by both the public
pages (public.py) and the admin Source list (admin.py). Keeping the mapping here — rather than
duplicating it in each layer or importing one route module from the other — means the "HN · 热门"
labels the two layers render can never drift apart."""
from __future__ import annotations

HN_FEED_LABELS = {
    "top": "热门",
    "best": "最佳",
    "new": "最新",
    "ask": "问答",
    "show": "展示",
    "job": "招聘",
}


def hackernews_source_label(source_type: str, config: dict) -> str | None:
    """Return the display label for a Hacker News Source, or None for non-HN types. An
    unrecognized configured feed falls back to the raw feed value after the "HN · " prefix
    rather than crashing or exposing the source type."""
    if source_type == "hackernews_stories":
        feed = config.get("feed", "")
        return f"HN · {HN_FEED_LABELS.get(feed, feed)}"
    if source_type == "hackernews_query":
        return f"HN 搜索 · {config.get('query', '')}"
    return None
