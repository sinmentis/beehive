from __future__ import annotations

from datetime import datetime, timezone


def parse_auction_closing_at(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        closing_at = datetime.fromisoformat(value)
    except ValueError:
        return None
    if closing_at.tzinfo is None:
        closing_at = closing_at.replace(tzinfo=timezone.utc)
    return closing_at.astimezone(timezone.utc)


def canonical_auction_closing_at(value: object) -> str | None:
    closing_at = parse_auction_closing_at(value)
    return closing_at.isoformat() if closing_at is not None else None


def format_auction_amount(value: object, currency_code: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    amount = f"{float(value):,.2f}".rstrip("0").rstrip(".")
    currency = currency_code.strip() if isinstance(currency_code, str) else ""
    return f"{currency} {amount}" if currency else amount
