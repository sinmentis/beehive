"""Claim and deliver due reminders for manually watched Tracker items."""
from __future__ import annotations

import html
import sqlite3
from datetime import datetime, timezone

from beehive.channels.tracker import adapter_for_source
from beehive.db.tracker_watches import (
    claim_due_tracker_reminders,
    complete_tracker_reminder_claim,
    fail_tracker_reminder_claim,
)
from beehive.email_routing import ResolvedRecipient
from beehive.localization import Localizer
from beehive.notify import Notifier
from beehive.scheduling import HOST_TZ


def _deadline_label(deadline: str) -> str:
    try:
        parsed = datetime.fromisoformat(deadline)
    except ValueError:
        return deadline
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(HOST_TZ).strftime("%Y-%m-%d %H:%M %Z")


def _render_reminder(items: list[dict], localizer: Localizer) -> tuple[str, str, str]:
    count = len(items)
    product = localizer.text("common.product_name")
    subject = localizer.text(
        "background.tracker_reminder.subject",
        count=count,
        product=product,
    )
    intro = localizer.text(
        "background.tracker_reminder.intro",
        count=count,
    )
    plain_blocks = []
    html_blocks = []
    for item in sorted(
        items,
        key=lambda candidate: (
            candidate["claimed_closing_at"],
            candidate["title"],
        ),
    ):
        display = adapter_for_source(item["source_type"]).display_facts(
            item["raw_metadata"], localizer
        )
        deadline = _deadline_label(item["claimed_closing_at"])
        details = list(display.details)
        if display.context:
            details.insert(
                0,
                localizer.text(
                    "background.tracker_reminder.context",
                    context=display.context,
                ),
            )
        details.append(
            localizer.text(
                "background.tracker_reminder.deadline",
                time=deadline,
            )
        )
        details.append(
            localizer.text(
                "background.tracker_reminder.view_item",
                url=item["url"],
            )
        )
        plain_blocks.append("\n".join((item["title"], *details)))

        detail_html = "".join(
            f"<li>{html.escape(detail)}</li>" for detail in details[:-1]
        )
        html_blocks.append(
            "<article>"
            f"<h2>{html.escape(item['title'])}</h2>"
            f"<ul>{detail_html}</ul>"
            f'<p><a href="{html.escape(item["url"], quote=True)}">'
            f"{html.escape(localizer.text('background.tracker_reminder.view_item_link'))}"
            "</a></p>"
            "</article>"
        )

    plain_text = f"{intro}\n\n" + "\n\n".join(plain_blocks)
    html_text = f"<h1>{html.escape(subject)}</h1><p>{html.escape(intro)}</p>" + "".join(
        html_blocks
    )
    return subject, plain_text, html_text


def send_due_tracker_reminders(
    conn: sqlite3.Connection,
    notifier: Notifier,
    default_recipient: ResolvedRecipient,
    localizer: Localizer,
    *,
    now: datetime | None = None,
) -> int:
    if default_recipient.address is None:
        print("[tracker-reminders] no default email recipient configured; skipping")
        return 0

    run_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    claim = claim_due_tracker_reminders(conn, run_time)
    if not claim.items or claim.token is None:
        return 0

    subject, plain_text, html_text = _render_reminder(claim.items, localizer)
    try:
        notifier.send(
            subject,
            plain_text,
            html_text,
            to_addr=default_recipient.address,
        )
    except Exception as exc:
        fail_tracker_reminder_claim(conn, claim.token, str(exc)[:2000])
        raise

    completed = complete_tracker_reminder_claim(conn, claim.token, run_time)
    if completed != len(claim.items):
        print(
            "[tracker-reminders] sent "
            f"{len(claim.items)} reminders but finalized {completed} watch rows"
        )
    return len(claim.items)
