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
from pathlib import Path
from urllib.parse import urlparse

from jinja2 import Environment, FileSystemLoader

from beehive.localization import Localizer

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_env = Environment(loader=FileSystemLoader(str(_TEMPLATES_DIR)), autoescape=True)


def _safe_href(url: str) -> str:
    return url if urlparse(url).scheme in ("http", "https") else "#"


_env.filters["safe_href"] = _safe_href


# Kind-specific accent (section rule + label badge colour) so an email visibly separates an
# Editorial signal from a Monitor deal from a Tracker deadline without exposing the raw kind.
_KIND_ACCENTS: dict[str, str] = {
    "editorial": "#4f46e5",
    "monitor": "#0f766e",
    "tracker": "#b45309",
}
_DEFAULT_ACCENT = "#4f46e5"


def _accent_for_kind(kind: str) -> str:
    return _KIND_ACCENTS.get(kind, _DEFAULT_ACCENT)


def _format_amount(value: object) -> str:
    """A price payload number as a compact human string: 40.0 -> "40", 39.9 -> "39.9",
    39.99 -> "39.99". Non-numbers (a malformed payload) fall back to str() so rendering never
    raises. bool is treated as non-numeric -- it is never a real price."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    number = float(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:.2f}".rstrip("0").rstrip(".")


@dataclass(frozen=True)
class EventView:
    """Presentation-ready view of one deliverable item event. All strings are raw (unescaped):
    the plain-text renderer needs them raw and the HTML template escapes them via Jinja
    autoescape. `label` is "" for an editorial discovery (the familiar linked summary carries no
    badge); `detail` is "" when the event has no secondary line (a price change or closing time)."""

    headline: str
    url: str
    label: str
    detail: str
    kind: str
    event_type: str


@dataclass(frozen=True)
class ChannelDigest:
    channel_name: str
    channel_kind: str
    events: list[EventView]
    source_warnings: list[str]
    accent: str = _DEFAULT_ACCENT


def build_event_view(event: dict, localizer: Localizer) -> EventView:
    """Turn one raw deliverable event row (from db.item_events.list_ready_events_for_channels)
    into a localized EventView. This is the ONE place the (Channel kind, event type) matrix is
    resolved into a label/detail, so neither send.py nor the templates branch on kind."""
    kind = event.get("channel_kind", "")
    event_type = event.get("event_type", "")
    headline = event.get("item_ai_summary") or event.get("item_title") or ""
    url = event.get("item_url") or ""

    label = ""
    detail = ""
    if event_type == "price_drop":
        label = localizer.text("background.digest_event_price_drop")
        payload = event.get("payload") or {}
        if "old_price" in payload and "new_price" in payload:
            detail = localizer.text(
                "background.digest_event_price_detail",
                old=_format_amount(payload["old_price"]),
                new=_format_amount(payload["new_price"]),
            )
    elif event_type == "back_in_stock":
        label = localizer.text("background.digest_event_back_in_stock")
    elif event_type == "discovered":
        if kind == "tracker":
            label = localizer.text("background.digest_event_tracked_new")
            closing_at = (event.get("item_raw_metadata") or {}).get("closing_at")
            if closing_at:
                detail = localizer.text(
                    "background.digest_event_closing", time=str(closing_at)
                )
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
    )


def compose_channel_digest(
    channel_name: str,
    channel_kind: str,
    events: list[dict],
    source_warnings: list[str],
    localizer: Localizer,
) -> ChannelDigest:
    """Build one Channel's section from its already-capped, already-ordered deliverable events.
    The caller (send.py) owns the highlight_count cap because it must mark exactly the included
    event ids delivered; this function renders whatever it is given, in order."""
    views = [build_event_view(event, localizer) for event in events]
    return ChannelDigest(
        channel_name=channel_name,
        channel_kind=channel_kind,
        events=views,
        source_warnings=source_warnings,
        accent=_accent_for_kind(channel_kind),
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
    for cd in channel_digests:
        lines.append(f"== {cd.channel_name} ==")
        for warning in cd.source_warnings:
            lines.append(f"! {warning}")
        for event in cd.events:
            lines.append(f"- {_event_meta_prefix(event)}{event.headline} ({event.url})")
        lines.append("")
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
    )
