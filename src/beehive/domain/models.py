"""Pure domain types. No framework/db/http imports — this module is the innermost layer
every other layer depends on, never the reverse."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class Channel:
    id: int | None
    name: str
    profile: str
    fetch_interval_hours: int


@dataclass(frozen=True)
class Source:
    id: int | None
    channel_id: int
    type: str
    config: dict
    last_fetch_at: datetime | None = None
    last_fetch_error: str | None = None


@dataclass(frozen=True)
class Item:
    id: int | None
    source_id: int
    external_id: str
    title: str
    url: str
    body: str
    created_at: datetime | None
    fetched_at: datetime
    ai_score: float | None
    ai_summary: str | None
    ai_rationale: str | None
    is_read: bool
    raw_metadata: dict = field(default_factory=dict)
