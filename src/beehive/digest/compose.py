"""One digest per email-group send, built entirely from actionable item_events (see
db/item_events.py), never from an item's fetched_at watermark. The caller
(send_email_group_digests) selects the ready, unsuppressed, undelivered events for a group's
Channels, caps each Channel at its highlight_count, and hands the survivors here already grouped
per Channel. This module's only job is to turn each raw event row into a localized, kind-aware
presentation and render it.

build_event_view is the single place kind/event-type branching lives: a Monitor price drop, a
Tracker's new tracked lot with its closing time, an Editorial discovery's familiar linked summary
-- all of it is decided here so send.py stays free of `if kind == ...` presentation logic and the
templates stay logic-free. render_digest_email (plain text) and the view helpers are pure;
render_digest_email_html reads the digest_email.html template from disk via a module-level Jinja2
Environment, the one exception to "no I/O" in this module. The subject is always supplied by the
caller (an email group's own, already-formatted subject_template) rather than derived here."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader
from markupsafe import Markup, escape

from beehive.auction import format_auction_amount
from beehive.localization import Localizer
from beehive.scheduling import DEFAULT_SCHEDULE_TIMEZONE
from beehive.site_links import normalize_site_url

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_env = Environment(loader=FileSystemLoader(str(_TEMPLATES_DIR)), autoescape=True)


def _safe_href(url: str) -> str:
    try:
        return url if urlparse(url).scheme in ("http", "https") else "#"
    except ValueError:
        return "#"


def _source_domain(url: str) -> str:
    if _safe_href(url) == "#":
        return ""
    return (urlparse(url).hostname or "").removeprefix("www.")


_env.filters["safe_href"] = _safe_href
_env.filters["source_domain"] = _source_domain


def _format_amount(value: object, currency_code: object = None) -> str:
    """A price payload number as a compact human string: 40.0 -> "40", 39.9 -> "39.9",
    39.99 -> "39.99". Non-numbers (a malformed payload) fall back to str() so rendering never
    raises. bool is treated as non-numeric -- it is never a real price."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    return format_auction_amount(value, currency_code) or str(value)


@dataclass(frozen=True)
class EventView:
    """Presentation-ready event. Text stays raw for the plain-text renderer; detail_html
    contains only localized formatting with escaped values. label_in_metadata keeps a
    Tracker's event label secondary to its deadline."""

    headline: str
    url: str
    label: str
    detail: str
    kind: str
    event_type: str
    brief_url: str | None = None
    detail_html: Markup | None = None
    label_in_metadata: bool = False


@dataclass(frozen=True)
class ChannelDigest:
    channel_name: str
    channel_kind: str
    events: list[EventView]
    source_warnings: list[str]
    # One-line health summary ("4/4 sources OK · 1873 listings tracked"), so a quiet Channel
    # reads as "checked, nothing new" rather than looking broken.
    status: str = ""
    # "…and N more not shown" when eligible events were left for a later email by the cap.
    remaining_note: str = ""
    site_url: str | None = None
    channel_url: str | None = None


def _deadline_label(value: object, timezone_name: str) -> str | None:
    try:
        moment = datetime.fromisoformat(str(value))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(ZoneInfo(timezone_name)).strftime("%Y-%m-%d %H:%M %Z")
    except (ValueError, OverflowError):
        return None


def build_event_view(
    event: dict, localizer: Localizer, *,
    timezone_name: str = DEFAULT_SCHEDULE_TIMEZONE, brief_url: str | None = None,
) -> EventView:
    """Turn one raw deliverable event row (from db.item_events.list_ready_events_for_channels)
    into a localized EventView. This is the ONE place the (Channel kind, event type) matrix is
    resolved into a label/detail, so neither send.py nor the templates branch on kind."""
    kind = event.get("channel_kind", "")
    event_type = event.get("event_type", "")
    headline = event.get("item_ai_summary") or event.get("item_title") or ""
    url = event.get("item_url") or ""

    label = ""
    detail = ""
    detail_html = None
    label_in_metadata = False
    if event_type == "price_drop":
        label = localizer.text("background.digest_event_price_drop")
        payload = event.get("payload") or {}
        if "old_price" in payload and "new_price" in payload:
            currency_code = payload.get("currency_code")
            if not currency_code:
                currency_code = (event.get("item_raw_metadata") or {}).get("currency_code")
            old = _format_amount(payload["old_price"], currency_code)
            new = _format_amount(payload["new_price"], currency_code)
            detail = localizer.text("background.digest_event_price_detail", old=old, new=new)
            detail_html = Markup(localizer.text(
                "background.digest_event_price_detail",
                old=escape(old), new=Markup("<strong>{}</strong>").format(new),
            ))
    elif event_type == "back_in_stock":
        label = localizer.text("background.digest_event_back_in_stock")
    elif event_type == "discovered":
        if kind == "tracker":
            label = localizer.text("background.digest_event_tracked_new")
            closing_at = _deadline_label(
                (event.get("item_raw_metadata") or {}).get("closing_at"), timezone_name,
            )
            if closing_at:
                detail = localizer.text(
                    "background.digest_event_closing", time=closing_at,
                )
                date, time = closing_at.split(" ", 1)
                detail_html = Markup(localizer.text(
                    "background.digest_event_closing",
                    time=Markup(
                        '<span style="display:inline-block;">{}</span> '
                        '<span style="display:inline-block;">{}</span>'
                    ).format(date, time),
                ))
                label_in_metadata = True
        elif kind == "monitor":
            label = localizer.text("background.digest_event_new")
        # An editorial discovery renders as the familiar linked summary, no badge.

    return EventView(
        headline=headline,
        url=url,
        label=label,
        detail=detail,
        kind=kind,
        event_type=event_type,
        brief_url=brief_url,
        detail_html=detail_html,
        label_in_metadata=label_in_metadata,
    )


def compose_channel_digest(
    channel_name: str,
    channel_kind: str,
    events: list[dict],
    source_warnings: list[str],
    localizer: Localizer,
    *,
    status: str = "",
    remaining_count: int = 0,
    channel_id: int | None = None,
    site_url: str | None = None,
    saved_brief_ids: frozenset[int] = frozenset(),
    timezone_name: str = DEFAULT_SCHEDULE_TIMEZONE,
) -> ChannelDigest:
    """Build one Channel's section from its already-capped, already-ordered deliverable events.
    The caller (send.py) owns the highlight_count cap because it must mark exactly the included
    event ids delivered; this function renders whatever it is given, in order."""
    website = normalize_site_url(site_url)
    views = [
        build_event_view(
            event, localizer, timezone_name=timezone_name,
            brief_url=(
                f"{website}/items/{event['item_id']}/brief"
                + (f"?origin=channel&channel_id={channel_id}" if channel_id is not None else "")
                if website and channel_kind == "editorial" and event.get("item_id") in saved_brief_ids
                else None
            ),
        )
        for event in events
    ]
    remaining_note = (
        localizer.text("background.digest_more_events", count=remaining_count)
        if remaining_count
        else ""
    )
    return ChannelDigest(
        channel_name=channel_name,
        channel_kind=channel_kind,
        events=views,
        source_warnings=source_warnings,
        status=status,
        remaining_note=remaining_note,
        site_url=website,
        channel_url=(
            f"{website}/channels/{channel_id}" + ("?show_read=1" if channel_kind == "editorial" else "")
            if website and channel_id is not None else None
        ),
    )


def _event_meta_prefix(event: EventView) -> str:
    """The bracketed "[New]" / "[Price drop \u00b7 40 \u2192 35]" tag a plain-text line carries so
    the semantics stay legible without any raw JSON; "" for an editorial discovery."""
    parts = [part for part in (event.label, event.detail) if part]
    if not parts:
        return ""
    return f"[{' \u00b7 '.join(parts)}] "


def render_digest_email(channel_digests: list[ChannelDigest], today_iso: str,
                        localizer: Localizer, subject: str) -> tuple[str, str]:
    lines = []
    website = next((digest.site_url for digest in channel_digests if digest.site_url), None)
    if website:
        lines.extend([
            f"{localizer.text('background.digest_open_site', product=localizer.text('common.product_name'))}: {website}",
            "",
        ])
    for cd in channel_digests:
        lines.append(f"== {cd.channel_name} ==")
        if cd.channel_url:
            lines.append(f"{localizer.text('background.digest_open_channel')}: {cd.channel_url}")
        if cd.status:
            lines.append(cd.status)
        for warning in cd.source_warnings:
            lines.append(f"! {warning}")
        for event in cd.events:
            lines.append(f"- {_event_meta_prefix(event)}{event.headline} ({event.url})")
            if event.brief_url:
                lines.append(f"  {localizer.text('background.digest_open_brief')}: {event.brief_url}")
        if cd.remaining_note:
            lines.append(cd.remaining_note)
        lines.append("")
    if channel_digests:
        lines.append(localizer.text("background.digest_snapshot_note"))
    return subject, "\n".join(lines).rstrip()


def render_digest_email_html(channel_digests: list[ChannelDigest], today_iso: str,
                             localizer: Localizer, subject: str) -> str:
    template = _env.get_template("digest_email.html")
    product = localizer.text("common.product_name")
    return template.render(
        channel_digests=channel_digests,
        today_iso=today_iso,
        lang=localizer.html_lang,
        page_title=subject,
        header_text=localizer.text("background.digest_header", product=product),
        site_url=next((digest.site_url for digest in channel_digests if digest.site_url), None),
        site_link_label=localizer.text("background.digest_open_site", product=product),
        channel_link_label=lambda channel: localizer.text(
            "background.digest_view_channel", channel=channel,
        ),
        source_link_label=localizer.text("background.digest_open_source"),
        field_separator=localizer.text("background.digest_field_separator"),
        brief_link_label=localizer.text("background.digest_open_brief"),
        contents_label=localizer.text(
            "background.digest_contents",
            channels=len(channel_digests), count=sum(len(digest.events) for digest in channel_digests),
        ),
        snapshot_note=localizer.text("background.digest_snapshot_note"),
        empty_state=localizer.text("background.digest_empty_state"),
    )
