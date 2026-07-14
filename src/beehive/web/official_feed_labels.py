"""Single source of truth for the public label and icon of each official institution feed,
shared by the admin Source list (admin.py) and the public pages (public.py) so the two layers
can never render a different label or icon for the same Source type. Mirrors the pattern
hackernews_labels.py already uses for Hacker News."""
from __future__ import annotations

OFFICIAL_FEED_LABELS = {
    "rbnz_news": {"label": "RBNZ News", "icon": "🏦"},
    "nz_government_news": {"label": "NZ Government", "icon": "🇳🇿"},
    "federal_reserve_news": {"label": "Federal Reserve", "icon": "🏛️"},
}


def official_feed_label(source_type: str) -> str | None:
    entry = OFFICIAL_FEED_LABELS.get(source_type)
    return entry["label"] if entry is not None else None


def official_feed_icon(source_type: str) -> str | None:
    entry = OFFICIAL_FEED_LABELS.get(source_type)
    return entry["icon"] if entry is not None else None
