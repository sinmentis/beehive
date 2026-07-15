"""Trafilatura extraction from HTML that has *already been downloaded* -- this module never
performs network I/O. `trafilatura.extract(html_string)` only parses the string it is given
(see `trafilatura.utils.load_html`, which type-checks the input to `bytes`/`str`/an existing
lxml tree and never treats a string as a URL to fetch); the network boundary is exclusively
`fetch.py`'s `ArticleFetcher`. Nothing here persists the extracted text -- callers own that
decision, and are expected to hold it only for the duration of downstream LLM synthesis.

Trafilatura's output is whitespace-normalized and bounded to a hard character cap, then
classified as complete/partial/unusable so callers can render an honest result instead of
silently serving a truncated or paywall-stub brief as if it were the full article:

- UNUSABLE: extraction produced no meaningful text at all (empty page, non-article
  boilerplate, or Trafilatura's algorithms simply found nothing worth extracting).
- PARTIAL: there IS extracted text, but it is degraded in some deterministic, reportable
  way -- truncated at the network transport, truncated to fit this module's own character
  cap, truncated to fit a caller-supplied prompt token/char budget, truncated to fit a
  caller-supplied chunk size, or simply short/paywall-shaped (below a minimum usable
  length, or containing a recognized paywall phrase).
- COMPLETE: none of the above applied.

PartialReason values are deliberately independent of each other and evaluated in a fixed
order, so the same input always produces the same reason tuple -- useful both for tests and
for rendering a stable, specific warning to the end user rather than a generic "truncated"."""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Callable

import trafilatura

_DEFAULT_MAX_CHARS = 20_000
_DEFAULT_MIN_USABLE_CHARS = 200

# Deliberately conservative and case-insensitive: real publisher paywall copy varies, but
# these phrasings recur across the vast majority of metered/hard paywalls we see in practice.
_PAYWALL_MARKERS = (
    "subscribe to continue reading",
    "subscribe to read the full",
    "this content is for subscribers",
    "sign in to continue reading",
    "to continue reading, please",
    "you have reached your limit of free articles",
    "become a member to read",
    "create a free account to continue reading",
    "this article is for subscribers only",
)

_BLANK_LINES_RE = re.compile(r"\n{3,}")
_INLINE_WHITESPACE_RE = re.compile(r"[ \t\f\v]+")
_LINE_END_WHITESPACE_RE = re.compile(r"[ \t]+\n")


class ExtractionQuality(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNUSABLE = "unusable"


class PartialReason(str, Enum):
    STORED_SOURCE = "stored_source"
    TRANSPORT_TRUNCATED = "transport_truncated"
    EXTRACTION_TRUNCATED = "extraction_truncated"
    PROMPT_BUDGET_TRUNCATED = "prompt_budget_truncated"
    MAX_CHUNK_TRUNCATED = "max_chunk_truncated"
    SHORT_CONTENT = "short_content"
    PAYWALL_LIKE = "paywall_like"


@dataclass(frozen=True)
class ExtractionResult:
    quality: ExtractionQuality
    text: str
    reasons: tuple[PartialReason, ...]
    char_count: int


def _default_extract(html: str) -> str | None:
    return trafilatura.extract(
        html,
        output_format="txt",
        include_comments=False,
        include_tables=True,
        favor_precision=True,
        with_metadata=False,
    )


def _normalize_whitespace(raw: str) -> str:
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    text = _LINE_END_WHITESPACE_RE.sub("\n", text)
    text = _INLINE_WHITESPACE_RE.sub(" ", text)
    text = _BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()


def _has_paywall_marker(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _PAYWALL_MARKERS)


def extract_article_text(
    html: str,
    *,
    transport_truncated: bool = False,
    max_chars: int = _DEFAULT_MAX_CHARS,
    prompt_budget_chars: int | None = None,
    max_chunk_chars: int | None = None,
    min_usable_chars: int = _DEFAULT_MIN_USABLE_CHARS,
    extractor: Callable[[str], str | None] = _default_extract,
) -> ExtractionResult:
    """Extract, normalize, bound, and classify article text from already-downloaded HTML.

    transport_truncated should be set from FetchedArticle.truncated (fetch.py) -- it is the
    only signal about the download itself that this function cannot derive on its own.
    prompt_budget_chars and max_chunk_chars are optional caller-supplied caps (an LLM prompt
    budget, a chunking window) applied on top of this module's own max_chars, each recorded
    as its own PartialReason only when it actually truncates further."""
    raw = extractor(html)
    if not raw or not raw.strip():
        return ExtractionResult(quality=ExtractionQuality.UNUSABLE, text="", reasons=(), char_count=0)

    text = _normalize_whitespace(raw)
    if not text:
        return ExtractionResult(quality=ExtractionQuality.UNUSABLE, text="", reasons=(), char_count=0)

    reasons: list[PartialReason] = []
    if transport_truncated:
        reasons.append(PartialReason.TRANSPORT_TRUNCATED)

    if len(text) > max_chars:
        text = text[:max_chars]
        reasons.append(PartialReason.EXTRACTION_TRUNCATED)

    if prompt_budget_chars is not None and len(text) > prompt_budget_chars:
        text = text[:prompt_budget_chars]
        reasons.append(PartialReason.PROMPT_BUDGET_TRUNCATED)

    if max_chunk_chars is not None and len(text) > max_chunk_chars:
        text = text[:max_chunk_chars]
        reasons.append(PartialReason.MAX_CHUNK_TRUNCATED)

    if _has_paywall_marker(text):
        reasons.append(PartialReason.PAYWALL_LIKE)
    elif len(text) < min_usable_chars:
        reasons.append(PartialReason.SHORT_CONTENT)

    quality = ExtractionQuality.PARTIAL if reasons else ExtractionQuality.COMPLETE
    return ExtractionResult(quality=quality, text=text, reasons=tuple(reasons), char_count=len(text))
