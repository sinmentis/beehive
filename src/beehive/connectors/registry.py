from __future__ import annotations

from beehive.connectors.base import SourceConnector

_CONNECTORS: dict[str, SourceConnector] = {}


def register(connector: SourceConnector) -> None:
    _CONNECTORS[connector.type_key] = connector


def get(type_key: str) -> SourceConnector:
    try:
        return _CONNECTORS[type_key]
    except KeyError:
        raise ValueError(f"unknown Source type: {type_key!r}") from None


def is_registered(type_key: str) -> bool:
    return type_key in _CONNECTORS


def all_connectors() -> tuple[SourceConnector, ...]:
    """Every registered connector, in registration order. The Source/Channel compatibility
    policy enumerates this to answer "which Source types may a Channel of kind X hold" without
    reaching into the private registry dict. Returns the connector objects (each carries its own
    type_key and supported_channel_kinds) rather than a mutable view of the registry."""
    return tuple(_CONNECTORS.values())
