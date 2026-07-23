"""Pure detection of actionable Item events from one mutable-snapshot fetch. Given what a listing
looked like before and after a fetch -- and which EmailEventTypes the Channel's definition permits
-- these functions decide *what happened* (a discovery, a price drop, a return to stock) and the
payload that makes each event legible in an email. No database, connector, or wall clock: the
collector records the results into item_events and later gates them on AI scoring; nothing here
persists or times anything.

The permitted-events set is threaded through every function so a kind can only ever emit what its
ChannelDefinition allows -- a tracker (permitting only DISCOVERED) shares the mutable path with a
monitor yet never produces a price/stock event, because the definition, not this module, is the
authority on which events a kind may emit."""
from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field

from beehive.domain.channels import EmailEventType


@dataclass(frozen=True)
class DetectedEvent:
    """One actionable event ready to be staged: its type and the JSON-serializable payload of the
    numbers that make it legible in an email (empty when the Item's own row already carries the
    context, e.g. a discovery or a back-in-stock)."""

    event_type: EmailEventType
    payload: dict = field(default_factory=dict)


def _numeric(value: object) -> float | None:
    """value as a float when it is a real number, else None. bool is rejected (True/False are
    ints in Python but never a price), as are None and strings -- only a genuine numeric price
    can take part in a price-drop comparison."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def detect_discovered(
    permitted: Collection[EmailEventType],
) -> list[DetectedEvent]:
    """The event for a newly inserted Item: a single DISCOVERED when the definition permits it,
    otherwise nothing. The Item row itself carries title/url/price, so the payload stays empty."""
    if EmailEventType.DISCOVERED not in permitted:
        return []
    return [DetectedEvent(EmailEventType.DISCOVERED)]


def detect_snapshot_events(
    before_metadata: dict,
    after_metadata: dict,
    permitted: Collection[EmailEventType],
) -> list[DetectedEvent]:
    """Events for an existing Item refreshed in place, from its before/after raw_metadata.

    PRICE_DROP fires when a numeric current `price` is strictly below the numeric price last
    recorded (its old/new numbers go in the payload because the old price is otherwise gone).
    BACK_IN_STOCK fires when the previous `available` was explicitly False and the current is True
    -- the same transition whether the listing merely toggled availability or reappeared after a
    spell inactive, since reappearance only matters when the metadata establishes the false->true
    move. Each is emitted only when the definition permits its type."""
    events: list[DetectedEvent] = []

    if EmailEventType.PRICE_DROP in permitted:
        old_price = _numeric(before_metadata.get("price"))
        new_price = _numeric(after_metadata.get("price"))
        if old_price is not None and new_price is not None and new_price < old_price:
            events.append(
                DetectedEvent(
                    EmailEventType.PRICE_DROP,
                    {"old_price": old_price, "new_price": new_price},
                )
            )

    if EmailEventType.BACK_IN_STOCK in permitted:
        if before_metadata.get("available") is False and (
            after_metadata.get("available") is True
        ):
            events.append(DetectedEvent(EmailEventType.BACK_IN_STOCK))

    return events
