"""The plugin seam: a Source `type` maps to one SourceConnector. Adding Phase 2's
google_news_query or Phase 3's twitter_account is a new file calling register() — nothing
here, in db/, or in collector/ needs to change."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True)
class RawItem:
    external_id: str
    title: str
    url: str
    body: str = ""
    created_at: datetime | None = None
    raw_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CommentFetchTarget:
    external_id: str
    url: str
    raw_metadata: dict[str, Any]


class SourceConnector(Protocol):
    type_key: str

    def validate_config(self, config: dict) -> None:
        ...

    def fetch(self, config: dict) -> list[RawItem]:
        ...


class CommentSourceConnector(Protocol):
    """Documents the optional comment-fetching interface a connector may implement. This is
    purely a type/documentation aid: runtime discovery uses hasattr(connector, "fetch_comments"),
    so a connector opts in simply by defining the method, without inheriting from this Protocol."""

    def fetch_comments(self, target: CommentFetchTarget) -> list[str]:
        ...
