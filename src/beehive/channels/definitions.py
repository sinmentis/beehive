"""The immutable, declarative registry describing how each Channel kind behaves. Every
kind-specific decision -- which AI ranking path to drive, how items persist, whether read/unread
is surfaced, where the kind appears (Home/Archive), which panel template renders it, whether it
supports a manual Watch, and which regular Email Group events it emits -- reads from one
ChannelDefinition here rather than being spread across `if kind == "editorial"` branches or a
SourceConnector-style subclass hierarchy.

There is no mutable state and no update path: a Channel's kind is immutable, so a definition is
looked up by ChannelKind and never mutated. The module asserts at import time that exactly every
ChannelKind has one definition, so a newly added kind that forgets its declaration fails loudly
on first import instead of silently defaulting."""
from __future__ import annotations

from dataclasses import dataclass

from beehive.domain.channels import (
    ChannelKind,
    EmailEventType,
    LifecycleMode,
    PersistenceMode,
    RankingMode,
    ReadModel,
)


@dataclass(frozen=True)
class ChannelDefinition:
    """The full behavioral description of one Channel kind. A frozen record, deliberately not a
    class with per-flag getter methods: callers read the fields directly."""

    kind: ChannelKind
    ranking_mode: RankingMode
    persistence_mode: PersistenceMode
    lifecycle_mode: LifecycleMode
    read_model: ReadModel
    home_eligible: bool
    archive_eligible: bool
    panel_template: str
    manual_watch: bool
    email_event_types: frozenset[EmailEventType]


_DEFINITIONS: tuple[ChannelDefinition, ...] = (
    ChannelDefinition(
        kind=ChannelKind.EDITORIAL,
        ranking_mode=RankingMode.EDITORIAL,
        persistence_mode=PersistenceMode.APPEND,
        lifecycle_mode=LifecycleMode.FEED,
        read_model=ReadModel.TRACKED,
        home_eligible=True,
        archive_eligible=True,
        panel_template="channel_editorial.html",
        manual_watch=False,
        email_event_types=frozenset({EmailEventType.DISCOVERED}),
    ),
    ChannelDefinition(
        kind=ChannelKind.MONITOR,
        ranking_mode=RankingMode.LISTING,
        persistence_mode=PersistenceMode.MUTABLE_SNAPSHOT,
        lifecycle_mode=LifecycleMode.CATALOGUE,
        read_model=ReadModel.SNAPSHOT,
        home_eligible=False,
        archive_eligible=False,
        panel_template="channel_monitor.html",
        manual_watch=False,
        email_event_types=frozenset(
            {
                EmailEventType.DISCOVERED,
                EmailEventType.PRICE_DROP,
                EmailEventType.BACK_IN_STOCK,
            }
        ),
    ),
    ChannelDefinition(
        kind=ChannelKind.TRACKER,
        ranking_mode=RankingMode.LISTING,
        persistence_mode=PersistenceMode.MUTABLE_SNAPSHOT,
        lifecycle_mode=LifecycleMode.ACTIVE_HISTORY,
        read_model=ReadModel.SNAPSHOT,
        home_eligible=False,
        archive_eligible=False,
        panel_template="channel_tracker.html",
        manual_watch=True,
        email_event_types=frozenset({EmailEventType.DISCOVERED}),
    ),
)

_DEFINITIONS_BY_KIND: dict[ChannelKind, ChannelDefinition] = {
    definition.kind: definition for definition in _DEFINITIONS
}

if len(_DEFINITIONS_BY_KIND) != len(_DEFINITIONS):
    raise AssertionError("duplicate ChannelKind in channel definitions")
if set(_DEFINITIONS_BY_KIND) != set(ChannelKind):
    raise AssertionError("channel definitions must cover exactly every ChannelKind")


def all_definitions() -> tuple[ChannelDefinition, ...]:
    """Every ChannelDefinition in a stable display order (editorial, monitor, tracker). The
    admin New Channel form generates its kind options from this, so a new kind appears in the
    UI automatically once it has a definition."""
    return _DEFINITIONS


def get_definition(kind: ChannelKind) -> ChannelDefinition:
    try:
        return _DEFINITIONS_BY_KIND[kind]
    except KeyError:
        raise ValueError(f"unknown Channel kind: {kind!r}") from None


def require_channel_kind(value: object) -> ChannelKind:
    """Canonical validation for a stored/submitted kind string: returns the matching ChannelKind
    or raises ValueError. This is the single lookup db/channels.py and the web layer use instead
    of a private tuple of literals, so the accepted set can never drift from the definitions."""
    for definition in _DEFINITIONS:
        if definition.kind.value == value:
            return definition.kind
    raise ValueError(f"unknown Channel kind: {value!r}")
