"""Hard ceilings for the Civic AI stages, in one place (plan 7.4).

Every number here is a ceiling enforced in code after the fact, never a target the model is
trusted to respect. A prompt may ask for something smaller, but each value below is re-checked
against the actual response by `beehive.civic.candidate_policy`, the same soft-cap-versus-hard-
check split `beehive.research.limits` already uses.

The locale set is the other thing this module fixes. Chinese is the canonical editorial base
and English, Hindi and Tagalog are overlays of it; all four are mandatory, and there is no
fallback locale anywhere in this package. A Candidate missing any one of them cannot be verified
and cannot be sealed, because a partially localized statement published to a multilingual reader
is a worse failure than publishing nothing.
"""
from __future__ import annotations

from enum import Enum


class CivicLocale(str, Enum):
    """The four mandatory PartyMap locales. `ZH` is the canonical base the other three overlay.

    Declared in generation order, which is also the order every deterministic parity check walks
    them in, so a diff between two locales is always reported the same way round."""

    ZH = "zh"
    EN = "en"
    HI = "hi"
    TL = "tl"

    @property
    def is_base(self) -> bool:
        return self is CivicLocale.ZH

    @property
    def llm_name(self) -> str:
        """What the prompt calls the language. Never abbreviated to the code: a model asked for
        'tl' has been observed to answer in Turkish."""
        return _LLM_NAMES[self]


_LLM_NAMES = {
    CivicLocale.ZH: "Simplified Chinese",
    CivicLocale.EN: "English",
    CivicLocale.HI: "Hindi",
    CivicLocale.TL: "Tagalog",
}

# The canonical editorial base. Every overlay is generated from this locale's approved content,
# never from another overlay, so a drift in one overlay can never propagate into a third.
BASE_LOCALE = CivicLocale.ZH

# The three overlays, in fixed order.
OVERLAY_LOCALES: tuple[CivicLocale, ...] = tuple(
    locale for locale in CivicLocale if locale is not BASE_LOCALE)

# All four, in fixed order. A Candidate carrying anything less is incomplete by definition.
REQUIRED_LOCALES: tuple[CivicLocale, ...] = tuple(CivicLocale)

# --- Plan 7.4 ceilings ---------------------------------------------------------------------

# Every AI call one Civic Run may make, across every Candidate and every stage. Counted from the
# durable stage table rather than from a worker's own memory, so a crashed-and-resumed Run cannot
# quietly buy itself a second budget.
MAX_AI_CALLS_PER_RUN = 30

# Evidence items shown in one prompt. The snapshot is truncated to this many aliases, strongest
# tier first, so a Candidate with twenty sources still produces a bounded, reproducible prompt.
MAX_EVIDENCE_ITEMS_PER_PROMPT = 8

# Characters of source text per evidence item in a prompt. Matches the capture bound
# (`documents.MAX_EXCERPT_CHARS`): a release's main measures often follow its opening quotes, and
# a summary can only carry what the model was shown. Eight items keep a prompt under 32k chars.
MAX_EVIDENCE_TEXT_CHARS = 4000

# Citations one Candidate may carry, counted over the union of its claims' aliases.
MAX_CITATIONS_PER_CANDIDATE = 6

# Candidate title, per locale.
MAX_TITLE_CHARS = 120

# Candidate summary, per locale.
MAX_SUMMARY_CHARS = 600

# --- Bounds this package adds around those ---------------------------------------------------

# Claims in one Candidate's structured payload. Bounded so locale parity stays checkable: every
# overlay must carry exactly the base's claims, in the same order.
MAX_CLAIMS_PER_CANDIDATE = 6

# One claim's text, per locale. Smaller than the summary because a claim is a single fact.
MAX_CLAIM_CHARS = 300

# Aliases one claim may cite.
MAX_CITATIONS_PER_CLAIM = 4

# Canonical entity identifiers (party slugs, subject ids) one Candidate may name.
MAX_ENTITIES_PER_CANDIDATE = 8

# Free-text reasons a model may return alongside a structured decision. Bounded because they are
# stored and shown to the Owner.
MAX_REASON_CHARS = 300

# Structured issues one verifier report may carry.
MAX_VERIFIER_ISSUES = 12

# Attempts one AI stage may consume, including the first. Exactly one retry, and only for a
# transient transport failure: a response that fails schema, policy or verifier checks rejects
# the Candidate for this batch (plan 7.8/7.9) rather than being argued with, so there is no
# self-repair loop anywhere in this package.
MAX_AI_STAGE_ATTEMPTS = 2

# Wall-clock budget for one model call.
AI_CALL_TIMEOUT_SECONDS = 120.0

__all__ = [
    "AI_CALL_TIMEOUT_SECONDS",
    "BASE_LOCALE",
    "CivicLocale",
    "MAX_AI_CALLS_PER_RUN",
    "MAX_AI_STAGE_ATTEMPTS",
    "MAX_CITATIONS_PER_CANDIDATE",
    "MAX_CITATIONS_PER_CLAIM",
    "MAX_CLAIMS_PER_CANDIDATE",
    "MAX_CLAIM_CHARS",
    "MAX_ENTITIES_PER_CANDIDATE",
    "MAX_EVIDENCE_ITEMS_PER_PROMPT",
    "MAX_EVIDENCE_TEXT_CHARS",
    "MAX_REASON_CHARS",
    "MAX_SUMMARY_CHARS",
    "MAX_TITLE_CHARS",
    "MAX_VERIFIER_ISSUES",
    "OVERLAY_LOCALES",
    "REQUIRED_LOCALES",
]
