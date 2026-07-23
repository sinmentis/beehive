"""The single, fail-closed authority on which Source types a Channel of a given kind may hold.
Persistence (db.sources.create_source) and the collector both gate on this before a Source is
stored or fetched, and the admin UI reads source_types_for_kind to offer only compatible Source
types -- there is no other sanctioned way to decide Source/Channel compatibility.

Every decision fails closed. connector_supports_kind / assert_source_allowed raise ValueError for
an unknown Source type, for a connector that declares no supported kinds (or a malformed
declaration), and for a genuine Source/Channel mismatch -- never a silent "allow" or "deny".
source_types_for_kind raises for an unknown Channel kind and, when enumerating, only includes a
connector that positively declares support for that kind, so a connector with a missing or empty
declaration is offered for nothing rather than everything.

The connector modules are imported here for their registration side effect: the policy can only
enumerate and resolve Source types that are actually registered, so importing the policy must be
enough to make every recurring connector available."""
from __future__ import annotations

from beehive.connectors import (  # noqa: F401 (import side effect: registers the connectors)
    all_about_auctions,
    google_news,
    hackernews,
    land_sea_collection,
    official_feeds,
    reddit,
    shopify_collection,
)
from beehive.connectors.base import SourceConnector
from beehive.connectors.registry import all_connectors
from beehive.connectors.registry import get as get_connector
from beehive.domain.channels import ChannelKind


def _require_channel_kind(kind: object) -> ChannelKind:
    if not isinstance(kind, ChannelKind):
        raise ValueError(f"unknown Channel kind: {kind!r}")
    return kind


def _declared_kinds(connector: SourceConnector, *, type_key: str) -> frozenset[ChannelKind]:
    """The connector's supported_channel_kinds, validated strictly. Raises for a missing, empty,
    or malformed declaration so a connector can never be treated as attachable to every kind or
    to a non-kind value by accident."""
    declared = getattr(connector, "supported_channel_kinds", None)
    if declared is None:
        raise ValueError(f"Source type {type_key!r} declares no supported_channel_kinds")
    if not isinstance(declared, frozenset):
        raise ValueError(
            f"Source type {type_key!r} supported_channel_kinds must be a frozenset, "
            f"got {type(declared).__name__}")
    if not declared:
        raise ValueError(f"Source type {type_key!r} supported_channel_kinds is empty")
    for member in declared:
        if not isinstance(member, ChannelKind):
            raise ValueError(
                f"Source type {type_key!r} supported_channel_kinds contains a non-ChannelKind "
                f"value: {member!r}")
    return declared


def connector_supports_kind(source_type: str, kind: ChannelKind) -> bool:
    """Whether one Source type may attach to a Channel of `kind`. Raises ValueError for an
    unknown Source type or a connector missing its declaration -- callers get a clear failure,
    never a silent False."""
    kind = _require_channel_kind(kind)
    connector = get_connector(source_type)
    return kind in _declared_kinds(connector, type_key=source_type)


def assert_source_allowed(source_type: str, kind: ChannelKind) -> None:
    """Raise ValueError unless `source_type` may attach to a Channel of `kind`. This is the gate
    db.sources.create_source and the collector call before persisting or fetching a Source."""
    if not connector_supports_kind(source_type, kind):
        raise ValueError(
            f"Source type {source_type!r} is not compatible with a {kind.value!r} Channel")


def source_types_for_kind(kind: ChannelKind) -> tuple[str, ...]:
    """Every registered Source type compatible with a Channel of `kind`, in registration order.
    Raises for an unknown kind. A connector is included only if it positively declares support
    for the kind; one with a missing/empty declaration is silently excluded (offered for no
    kind) rather than crashing the whole enumeration."""
    kind = _require_channel_kind(kind)
    matched: list[str] = []
    for connector in all_connectors():
        try:
            declared = _declared_kinds(connector, type_key=connector.type_key)
        except ValueError:
            continue
        if kind in declared:
            matched.append(connector.type_key)
    return tuple(matched)


def assert_registered_connector_integrity() -> None:
    """Validate every connector loaded at process startup.

    Enumeration remains tolerant so one malformed test/plugin connector is offered for no kind,
    but production startup must fail loudly rather than leave a configured Source unusable.
    """

    for connector in all_connectors():
        declared = _declared_kinds(connector, type_key=connector.type_key)
        if ChannelKind.TRACKER in declared:
            adapter = getattr(connector, "tracker_adapter", None)
            if adapter is None:
                raise ValueError(
                    f"Tracker Source type {connector.type_key!r} declares no tracker_adapter"
                )
            if not callable(getattr(adapter, "facts", None)) or not callable(
                getattr(adapter, "display_facts", None)
            ):
                raise ValueError(
                    f"Tracker Source type {connector.type_key!r} declares a malformed "
                    "tracker_adapter"
                )


assert_registered_connector_integrity()
