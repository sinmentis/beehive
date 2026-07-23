"""Backward-compatible auction names for the generic Tracker watch store."""
from beehive.db.tracker_watches import (
    TrackerReminderClaim as AuctionReminderClaim,
)
from beehive.db.tracker_watches import (
    add_tracker_watch as add_auction_watch,
)
from beehive.db.tracker_watches import (
    claim_due_tracker_reminders as claim_due_auction_reminders,
)
from beehive.db.tracker_watches import (
    complete_tracker_reminder_claim as complete_auction_reminder_claim,
)
from beehive.db.tracker_watches import (
    fail_tracker_reminder_claim as fail_auction_reminder_claim,
)
from beehive.db.tracker_watches import get_watched_item_ids
from beehive.db.tracker_watches import (
    list_tracker_watches as list_auction_watches,
)
from beehive.db.tracker_watches import (
    remove_tracker_watch as remove_auction_watch,
)

__all__ = [
    "AuctionReminderClaim",
    "add_auction_watch",
    "claim_due_auction_reminders",
    "complete_auction_reminder_claim",
    "fail_auction_reminder_claim",
    "get_watched_item_ids",
    "list_auction_watches",
    "remove_auction_watch",
]
