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
