"""Exactly one digest per day regardless of fetch frequency; "new since last
digest" is decided by the caller (Task 16), which only passes items fetched after the last
successful send. A Channel with nothing new still renders a reassuring line, never silence —
this is the direct anti-FOMO mechanism, not filler. render_digest_email (plain text) is a pure
function; render_digest_email_html reads the digest_email.html template file from disk via a
module-level Jinja2 Environment, the one exception to "no I/O" in this module."""
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


@dataclass(frozen=True)
class ChannelDigest:
    channel_name: str
    highlighted: list[dict]
    source_warnings: list[str]


def compose_channel_digest(channel_name: str, new_items: list[dict], source_warnings: list[str],
                           highlight_count: int = 8) -> ChannelDigest:
    return ChannelDigest(channel_name=channel_name, highlighted=new_items[:highlight_count],
                          source_warnings=source_warnings)


def render_digest_email(channel_digests: list[ChannelDigest], today_iso: str,
                        localizer: Localizer) -> tuple[str, str]:
    product = localizer.text("common.product_name")
    subject = localizer.text("background.digest_title", product=product, date=today_iso)
    empty_state = localizer.text("background.digest_empty_state")
    lines = []
    for cd in channel_digests:
        lines.append(f"== {cd.channel_name} ==")
        for warning in cd.source_warnings:
            lines.append(f"⚠ {warning}")
        if not cd.highlighted:
            lines.append(empty_state)
        else:
            for item in cd.highlighted:
                lines.append(f"- {item['ai_summary']} ({item['url']})")
        lines.append("")
    return subject, "\n".join(lines).rstrip()


def render_digest_email_html(channel_digests: list[ChannelDigest], today_iso: str,
                             localizer: Localizer) -> str:
    template = _env.get_template("digest_email.html")
    product = localizer.text("common.product_name")
    return template.render(
        channel_digests=channel_digests,
        today_iso=today_iso,
        lang=localizer.html_lang,
        page_title=localizer.text("background.digest_title", product=product, date=today_iso),
        header_text=localizer.text("background.digest_header", product=product),
        empty_state_text=localizer.text("background.digest_empty_state"),
    )
