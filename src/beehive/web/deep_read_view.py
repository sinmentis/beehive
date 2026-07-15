"""View-model + presentation helpers bridging beehive.db.deep_reads' raw DeepRead rows and (a)
the public/optional-session deep-read brief page, (b) its small HTMX status partial, and (c)
list-view (Dashboard/Channel/Archive) item decoration. Kept separate from web/public.py so those
route bodies stay thin and so this module's two safety-sensitive jobs live in exactly one place:

1. Strictly parse a stored `result_json` into a validated, template-safe shape. A malformed cache
   entry (corrupt JSON, missing/mistyped field) must degrade to the same localized
   "unavailable" copy any other failure uses -- never a raw exception, a stack trace, or the raw
   JSON reaching a response.
2. Build the dedicated brief page's URL (and its own polling status URL) from ONLY allowlisted
   values -- `origin` must already be a member of ALLOWED_ORIGINS and `channel_id` only appears
   when origin == "channel" -- so a redirect/link built here can never be steered by an
   unvalidated request value (open-redirect / header-injection hardening).

`error_detail` (from db.deep_reads.DeepRead) is intentionally never read by this module -- only
`error_code` feeds a localized, safe message. Any future change here must keep that invariant.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import urlencode

from beehive.db.deep_reads import DeepRead
from beehive.localization import SUPPORTED_LANGUAGES, Localizer
from beehive.web.formatting import host_local_time_label

ALLOWED_ORIGINS = ("dashboard", "channel", "archive")

_LANGUAGE_NATIVE_NAMES = {language.code: language.native_name for language in SUPPORTED_LANGUAGES}


def _language_display_name(language_code: str | None) -> str | None:
    if language_code is None:
        return None
    return _LANGUAGE_NATIVE_NAMES.get(language_code, language_code)

_FAILURE_TRANSLATION_KEYS = {
    "fetch": "web.deep_read.failure_fetch",
    "extraction": "web.deep_read.failure_extraction",
    "llm": "web.deep_read.failure_llm",
    "unavailable": "web.deep_read.failure_unavailable",
}
_FALLBACK_FAILURE_KEY = "web.deep_read.failure_unavailable"


@dataclass(frozen=True)
class ImportantFigureView:
    value: str
    label: str


@dataclass(frozen=True)
class DeepReadBriefView:
    """Validated, template-ready shape of one ready deep-read result. Field names mirror
    beehive.deep_read.summarize.DeepReadResult (minus its internal `item_id`, which is never
    rendered), but this dataclass is intentionally re-declared here rather than imported --
    web/ must never import from deep_read/ (that module belongs to the worker/AI side), and
    parse_deep_read_result below re-validates every field from scratch rather than trusting the
    stored JSON matches that contract."""
    bottom_line: str
    key_findings: list[str]
    important_figures: list[ImportantFigureView]
    why_it_matters: str
    limitations: str


class DeepReadCacheError(Exception):
    """Raised by parse_deep_read_result when a stored result_json fails strict validation.
    Callers must catch this and render the localized generic-unavailable failure copy -- never
    the exception message and never the raw stored JSON."""


def parse_deep_read_result(result_json: str, expected_item_id: int) -> DeepReadBriefView:
    """Strictly validates a stored result_json against the item it is being rendered for.
    beehive.deep_read.summarize.parse_deep_read_response already stores `item_id` as
    `str(expected_item_id)` at generation time (see its own expected_id_str check), so a
    missing or mismatched item_id here means either a corrupted row or -- since deep_reads is
    keyed 1:1 by item_id (schema.sql PK) this should never legitimately happen -- a defensive
    signal that the cached payload does not actually belong to this item. Either way this must
    be treated exactly like any other malformed cache: DeepReadCacheError, never a raw
    cross-item leak rendered to the page.

    Strictness rules: `bottom_line`/`why_it_matters` must be non-empty after stripping
    whitespace; `key_findings` must be a non-empty list of non-whitespace-only strings; every
    `important_figures` entry must have non-whitespace-only string `value`/`label` (the list
    itself MAY be empty -- "no notable figures" is a legitimate result, rendered via its own
    fallback copy). `limitations` is the one field allowed to be an empty string -- a
    genuinely-empty limitations section is legitimate output, not a sign of a malformed cache."""
    try:
        data = json.loads(result_json)
    except (TypeError, ValueError) as exc:
        raise DeepReadCacheError("result_json is not valid JSON") from exc
    if not isinstance(data, dict):
        raise DeepReadCacheError("result_json is not a JSON object")

    stored_item_id = data.get("item_id")
    if stored_item_id is None or str(stored_item_id) != str(expected_item_id):
        raise DeepReadCacheError(
            f"result_json item_id {stored_item_id!r} does not match expected {expected_item_id!r}")

    try:
        bottom_line = data["bottom_line"]
        key_findings = data["key_findings"]
        important_figures_raw = data["important_figures"]
        why_it_matters = data["why_it_matters"]
        limitations = data["limitations"]
    except KeyError as exc:
        raise DeepReadCacheError(f"result_json is missing field {exc}") from exc

    if not isinstance(bottom_line, str) or not bottom_line.strip():
        raise DeepReadCacheError("bottom_line must be a non-empty string")
    if not isinstance(why_it_matters, str) or not why_it_matters.strip():
        raise DeepReadCacheError("why_it_matters must be a non-empty string")
    if not isinstance(limitations, str):
        raise DeepReadCacheError("limitations must be a string")
    if not isinstance(key_findings, list) or not key_findings:
        raise DeepReadCacheError("key_findings must be a non-empty list of strings")
    if not all(isinstance(k, str) and k.strip() for k in key_findings):
        raise DeepReadCacheError("key_findings entries must be non-empty strings")
    if not isinstance(important_figures_raw, list):
        raise DeepReadCacheError("important_figures must be a list")

    figures = []
    for figure in important_figures_raw:
        value = figure.get("value") if isinstance(figure, dict) else None
        label = figure.get("label") if isinstance(figure, dict) else None
        if (not isinstance(figure, dict)
                or not isinstance(value, str) or not value.strip()
                or not isinstance(label, str) or not label.strip()):
            raise DeepReadCacheError(
                "important_figures entries must have non-empty string value/label")
        figures.append(ImportantFigureView(value=value, label=label))

    return DeepReadBriefView(
        bottom_line=bottom_line,
        key_findings=list(key_findings),
        important_figures=figures,
        why_it_matters=why_it_matters,
        limitations=limitations,
    )


def failure_message(t: Localizer, error_code: str | None) -> str:
    """Maps a typed, worker-assigned error_code to localized, safe copy -- `error_detail` (the
    only field that might carry raw exception text/attacker-influenced fetch failure detail) is
    never consulted here or anywhere else in this module."""
    key = _FAILURE_TRANSLATION_KEYS.get(error_code or "", _FALLBACK_FAILURE_KEY)
    return t.text(key)


def brief_url(item_id: int, origin: str | None, channel_id: int | None) -> str:
    """Builds the dedicated brief page's URL from ONLY allowlisted values: `origin` is used
    only if it is already a member of ALLOWED_ORIGINS (otherwise silently omitted, never
    reflected raw), and `channel_id` is only included when origin == "channel". Callers must
    validate origin/channel_id against real data (e.g. an existing channel) before calling this;
    this function's own job is narrower -- never emit a query string built from an unvalidated
    request value."""
    query: dict[str, str | int] = {}
    if origin in ALLOWED_ORIGINS:
        query["origin"] = origin
        if origin == "channel" and channel_id is not None:
            query["channel_id"] = channel_id
    suffix = f"?{urlencode(query)}" if query else ""
    return f"/items/{item_id}/brief{suffix}"


def status_url(item_id: int, origin: str | None, channel_id: int | None) -> str:
    query: dict[str, str | int] = {}
    if origin in ALLOWED_ORIGINS:
        query["origin"] = origin
        if origin == "channel" and channel_id is not None:
            query["channel_id"] = channel_id
    suffix = f"?{urlencode(query)}" if query else ""
    return f"/items/{item_id}/brief/status{suffix}"


def back_link(t: Localizer, origin: str | None, channel_id: int | None,
              channel_name: str | None) -> dict:
    """The brief page's single "back to where you came from" link. Falls back to the generic
    Dashboard-pointing default whenever origin is missing/unrecognized or (for "channel") the
    channel could no longer be resolved -- never builds a link from an unvalidated value."""
    if origin == "dashboard":
        return {"href": "/", "label": t.text("web.deep_read.back_to_dashboard")}
    if origin == "channel" and channel_id is not None and channel_name is not None:
        return {
            "href": f"/channels/{channel_id}",
            "label": t.text("web.deep_read.back_to_channel", channel=channel_name),
        }
    if origin == "archive":
        return {"href": "/archive", "label": t.text("web.deep_read.back_to_archive")}
    return {"href": "/", "label": t.text("web.deep_read.back_default")}


def build_brief_context(*, item: dict, deep_read: DeepRead | None, is_owner: bool,
                         origin: str | None, channel_id: int | None,
                         channel_name: str | None, csrf_token: str | None,
                         t: Localizer) -> dict:
    """Assembles the full template context shared by the dedicated brief page and its HTMX
    status partial. `status` reflects the DISPLAY state (a malformed 'ready' cache entry is
    downgraded to 'failed' for rendering purposes), while callers that need the raw DB status
    (e.g. to decide whether to keep polling) should read `deep_read.status` directly."""
    raw_status = deep_read.status if deep_read is not None else "not_requested"
    status = raw_status
    brief = None
    warning_text = None
    failure_text = None

    if raw_status == "ready" and deep_read is not None and deep_read.result_json is not None:
        try:
            brief = parse_deep_read_result(deep_read.result_json, item["id"])
        except DeepReadCacheError:
            status = "failed"
            failure_text = t.text(_FALLBACK_FAILURE_KEY)
        else:
            if deep_read.warning_code == "content_incomplete":
                warning_text = t.text("web.deep_read.incomplete_warning")
    elif raw_status == "failed" and deep_read is not None:
        failure_text = failure_message(t, deep_read.error_code)

    return {
        "item": item,
        "status": status,
        "brief": brief,
        "warning_text": warning_text,
        "failure_text": failure_text,
        "language_code": deep_read.language_code if deep_read is not None else None,
        "language_display_name": _language_display_name(
            deep_read.language_code if deep_read is not None else None),
        "generated_at": (
            host_local_time_label(deep_read.completed_at)
            if deep_read is not None and deep_read.completed_at is not None else None
        ),
        "is_owner": is_owner,
        "csrf_token": csrf_token,
        "origin": origin,
        # form_origin is the value any owner-control form on this page submits back as
        # `origin`: always a member of ALLOWED_ORIGINS (falls back to "dashboard" when the page
        # was reached without a resolvable origin, e.g. a bookmarked/direct link) so a
        # retry/regenerate submission is never rejected by the POST route's own origin allowlist
        # check purely because the GET request lost its back-navigation context.
        "form_origin": origin if origin in ALLOWED_ORIGINS else "dashboard",
        "channel_id": channel_id if origin == "channel" else None,
        "back_link": back_link(t, origin, channel_id, channel_name),
        "request_url": f"/items/{item['id']}/deep-read",
        "status_url": status_url(item["id"], origin, channel_id),
        "brief_url": brief_url(item["id"], origin, channel_id),
        "can_start": is_owner and status == "not_requested",
        "can_regenerate": is_owner and status in ("ready", "failed"),
        "is_pending": status in ("pending", "processing"),
    }


def decorate_deep_read_state(item: dict, deep_read: DeepRead | None, is_owner: bool,
                              origin: str, channel_id: int | None,
                              csrf_token: str | None) -> None:
    """List-view (Dashboard/Channel/Archive) decoration: attaches an item["deep_read"] state/
    action bundle for the dependent UI todo to render, WITHOUT touching any of the item's own
    read/open/vote fields. Only ranked items (ai_score is not None) get a bundle -- an unranked
    item can never have a deep-read row (the request route and the worker both reject it), so
    item["deep_read"] is left as None for those."""
    if item.get("ai_score") is None:
        item["deep_read"] = None
        return
    status = deep_read.status if deep_read is not None else "not_requested"
    item["deep_read"] = {
        "status": status,
        "origin": origin,
        "channel_id": channel_id if origin == "channel" else None,
        "csrf_token": csrf_token if is_owner else None,
        "request_url": f"/items/{item['id']}/deep-read",
        "brief_url": brief_url(item["id"], origin, channel_id),
        "can_start": is_owner and status == "not_requested",
        "can_regenerate": is_owner and status in ("ready", "failed"),
        "is_pending": status in ("pending", "processing"),
        "is_ready": status == "ready",
        "is_failed": status == "failed",
    }
