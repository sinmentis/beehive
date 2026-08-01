"""The plugin seam: a Source `type` maps to one SourceConnector. Adding Phase 2's
google_news_query or Phase 3's twitter_account is a new file calling register() — nothing
here, in db/, or in collector/ needs to change."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

from beehive.domain.channels import ChannelKind


def as_utc(value: datetime | None) -> datetime | None:
    """Normalizes a connector-parsed timestamp to an aware UTC datetime.

    Every connector must put its `created_at` through this. `email.utils.parsedate_to_datetime`
    returns a NAIVE datetime for the very common `-0000`/`GMT` forms, and `RawItem.created_at` is
    persisted with `.isoformat()`, so a naive value is stored without an offset and later read
    back as if it were UTC. That is usually right by luck, but it made two things wrong: an
    offset-bearing feed and an offset-free one for the same item produced different stored
    strings, and `upsert_mutable_item` compares `created_at` as a raw string to detect a content
    change -- so a feed that alternates between the two forms marked every item changed on every
    cycle, nulling its `ai_score` and paying for a fresh LLM rank each time.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class TruncatedSnapshotError(RuntimeError):
    """A paginating connector hit its page cap while the source still had more to give.

    Raised instead of returning the short list, because a MUTABLE_SNAPSHOT Channel treats every
    successful fetch as *the complete current catalogue*: `Collection.ingest_fetch` reconciles and
    marks every listing absent from it inactive. A connector that quietly stopped at its page cap
    would therefore retire every listing past the cap on each cycle, then revive them the moment
    the cap moved -- silent, repeated data loss that looks exactly like the shop delisting stock.

    Failing the fetch instead means nothing is reconciled, the previous snapshot stays intact, and
    the operator sees the error on the Source (`last_fetch_error`) telling them to narrow the
    collection URL or raise the cap.
    """


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
    # The Channel kinds this connector's Source type may be attached to. This is a required,
    # non-empty declaration: the Source/Channel compatibility policy (channels/source_policy.py)
    # fails closed if it is missing or empty, so a new connector cannot be silently attachable to
    # every kind or to none. An editorial feed declares {EDITORIAL}, a storefront watch declares
    # {MONITOR}, an auction-lot tracker declares {TRACKER}.
    supported_channel_kinds: frozenset[ChannelKind]

    def validate_config(self, config: dict) -> None:
        ...

    def fetch(self, config: dict) -> list[RawItem]:
        ...


@runtime_checkable
class PreviewSourceConnector(Protocol):
    """Optional bounded fetch used by the admin Source test surface."""

    def fetch_preview(self, config: dict, *, limit: int) -> list[RawItem]:
        ...


class CommentSourceConnector(Protocol):
    """Documents the optional comment-fetching interface a connector may implement. This is
    purely a type/documentation aid: runtime discovery uses hasattr(connector, "fetch_comments"),
    so a connector opts in simply by defining the method, without inheriting from this Protocol."""

    def fetch_comments(self, target: CommentFetchTarget) -> list[str]:
        ...
