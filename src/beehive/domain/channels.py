"""Pure Channel workflow types.

No framework, database, connector, or HTTP imports belong here. A Channel's kind is chosen at
creation and remains immutable because it determines Source compatibility, item lifecycle,
presentation, and notification behavior.
"""
from __future__ import annotations

from enum import Enum


class ChannelKind(str, Enum):
    """The complete, closed set of stored Channel kinds. Anything outside this set is unknown
    and must be rejected by callers rather than defaulted."""

    EDITORIAL = "editorial"
    MONITOR = "monitor"
    TRACKER = "tracker"


class RankingMode(str, Enum):
    """Which AI ranking contract a Channel uses."""

    EDITORIAL = "editorial"
    LISTING = "listing"


class PersistenceMode(str, Enum):
    """How fetched items are persisted."""

    APPEND = "append"
    MUTABLE_SNAPSHOT = "mutable_snapshot"


class LifecycleMode(str, Enum):
    """How a panel partitions and retains its items."""

    FEED = "feed"
    CATALOGUE = "catalogue"
    ACTIVE_HISTORY = "active_history"


class ReadModel(str, Enum):
    """Whether per-item read/unread is a surfaced part of the reading model. TRACKED: editorial
    items are read, voted on, and archived as "signals to read". SNAPSHOT: monitor/tracker items
    are state snapshots where read/unread is not the primary lens."""

    TRACKED = "tracked"
    SNAPSHOT = "snapshot"


class EmailEventType(str, Enum):
    """Actionable item events eligible for regular Email Group delivery."""

    DISCOVERED = "discovered"
    PRICE_DROP = "price_drop"
    BACK_IN_STOCK = "back_in_stock"
