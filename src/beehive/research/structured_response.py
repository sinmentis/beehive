# src/beehive/research/structured_response.py
"""Shared strict-parsing primitives for every Research AI structured-JSON response (ADR-0007:
Research AI never executes connectors/tools -- it only returns inert JSON that this module's
helpers turn into typed, bounded application data). `planner.py`'s Research Plan parser is
built entirely from these primitives so "fenced JSON only", "exact top-level shape", and
"bounded strings/lists" are enforced identically for every Research AI response parser rather
than each one reinventing its own JSON-shape checking with slightly different edge cases.

Strict-raise discipline, mirroring ai/response_parser.py and deep_read/summarize.py: a
malformed or incomplete required field is a hard failure -- StructuredResponseError, never a
silent default/empty-value fallback and never a silently dropped unrecognized top-level key.
Bounding (max length / max list size) is the one place this module clips rather than raises,
and only for fields that already passed their required-shape check -- the same soft-cap
philosophy those two modules already use for display text."""
from __future__ import annotations

import json
import re

_FENCE_RE = re.compile(r"```json\s*(.*?)\s*```", re.DOTALL)


class StructuredResponseError(ValueError):
    """Raised for any Research AI response that fails strict structured-JSON parsing. Callers
    must treat this as a hard failure -- there is no silent fallback to a partial or default
    result."""


def extract_fenced_json_object(raw_text: str, *, context: str) -> dict:
    """Extracts the first fenced ```json ... ``` block and parses it as a JSON object.

    Rejects anything else: no fenced block at all, a fenced block that isn't valid JSON, and a
    valid-but-non-object top level (list, string, number, null) are all hard failures. Prose
    before/after the fence is ignored -- never parsed as instructions."""
    match = _FENCE_RE.search(raw_text)
    if match is None:
        raise StructuredResponseError(f"no fenced ```json block found in {context} response")
    try:
        parsed = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise StructuredResponseError(f"{context} fenced block is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise StructuredResponseError(
            f"{context} JSON block must be a JSON object, got {type(parsed).__name__}")
    return parsed


def require_exact_keys(parsed: dict, *, allowed_keys: frozenset[str], context: str) -> None:
    """Rejects any key outside allowed_keys. An AI response that stuffs in an unexpected key
    (e.g. mimicking a "successful" prompt-injection payload, or simply hallucinating an extra
    field) fails parsing outright instead of that key being silently ignored."""
    unexpected = sorted(set(parsed) - allowed_keys)
    if unexpected:
        raise StructuredResponseError(f"{context} response has unexpected keys: {unexpected}")


def require_string(value: object, *, field: str, max_len: int, context: str) -> str:
    """Requires a non-empty string, stripped and capped at max_len.

    Raises for any missing, non-string, or blank (whitespace-only) value -- required text
    fields never fall back to an empty string."""
    if not isinstance(value, str) or not value.strip():
        raise StructuredResponseError(f"{context} response is missing a non-empty '{field}'")
    return value.strip()[:max_len]


def require_list(value: object, *, field: str, context: str) -> list:
    if not isinstance(value, list):
        raise StructuredResponseError(f"{context} response '{field}' must be a list")
    return value


def require_dict(value: object, *, field: str, context: str) -> dict:
    if not isinstance(value, dict):
        raise StructuredResponseError(f"{context} response '{field}' must be a JSON object")
    return value


def require_bool(value: object, *, field: str, context: str) -> bool:
    """Requires an actual JSON boolean. Rejects a missing value and, deliberately, also
    rejects the merely-bool-like values JSON/Python often conflate with booleans (0/1, "true"/
    "false" strings) -- isinstance(value, bool) is checked before isinstance(value, int)
    would otherwise also accept 0/1, since Python's bool is an int subclass."""
    if not isinstance(value, bool):
        raise StructuredResponseError(f"{context} response is missing a boolean '{field}'")
    return value


def bounded_string_list(
    value: object, *, field: str, max_items: int, max_item_len: int, context: str,
) -> list[str]:
    """Best-effort bounding for a list of display strings (e.g. Owner-visible gap text):
    non-string or blank entries are dropped rather than failing the whole response -- this is
    only ever used for fields whose partial content does not invalidate the rest of the
    response, matching the strict-raise/soft-cap split documented above."""
    items = require_list(value, field=field, context=context)
    result: list[str] = []
    for entry in items:
        if isinstance(entry, str) and entry.strip():
            result.append(entry.strip()[:max_item_len])
        if len(result) >= max_items:
            break
    return result
