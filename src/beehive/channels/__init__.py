"""Channel behavior package: the declarative ChannelDefinition registry (definitions.py) and the
fail-closed Source/Channel compatibility policy (source_policy.py).

The curated surface below is intentionally connector-free -- importing beehive.channels validates
and looks up Channel kinds without pulling in the connector registry. Callers that need Source
compatibility import beehive.channels.source_policy explicitly, which registers the connectors it
must enumerate."""
from __future__ import annotations

from beehive.channels.definitions import (
    ChannelDefinition,
    all_definitions,
    get_definition,
    require_channel_kind,
)

__all__ = [
    "ChannelDefinition",
    "all_definitions",
    "get_definition",
    "require_channel_kind",
]
