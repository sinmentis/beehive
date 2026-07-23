"""Backward-compatible auction name for the generic Tracker reminder worker."""
from beehive.tracker_reminders import (
    send_due_tracker_reminders as send_due_auction_reminders,
)

__all__ = ["send_due_auction_reminders"]
