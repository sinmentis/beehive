"""Strict parsers for every Civic AI stage response, and the typed payloads they produce.

Built entirely from `beehive.ai.structured_response`, so "first fenced JSON object", "exact
top-level keys", "bounded strings and lists" mean the same thing here as they do for Research.
What this module adds is the Civic-specific shape: closed decision vocabularies, per-locale
content with claim-level citation aliases, and a verifier report that can only ever say pass or
reject.

**Strict-raise, never repair.** A response that is missing a key, carries an extra key, uses a
value outside a closed vocabulary, or exceeds a bound is a hard failure. Nothing here coerces,
truncates a required field into validity, or falls back to a default: plan 7.8 makes any parser
failure a rejection of the Candidate for this batch, and a parser that quietly fixed a malformed
answer would be publishing something no stage actually approved.

**Aliases are strings here and resolved elsewhere.** A citation is parsed as an alias token and
checked for shape only. Whether it exists, whether it points at admissible evidence, and whether
the locales agree about it are `beehive.civic.candidate_policy`'s job, because those questions
need the snapshot and this module deliberately knows nothing about it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum

from beehive.ai.structured_response import (StructuredResponseError, extract_fenced_json_object,
                                            require_bool, require_dict, require_exact_keys,
                                            require_list, require_string)
from beehive.civic.limits import (MAX_CITATIONS_PER_CLAIM, MAX_CLAIM_CHARS,
                                  MAX_CLAIMS_PER_CANDIDATE, MAX_ENTITIES_PER_CANDIDATE,
                                  MAX_REASON_CHARS, MAX_SUMMARY_CHARS, MAX_TITLE_CHARS,
                                  MAX_VERIFIER_ISSUES, CivicLocale)


class CivicScreeningDecision(str, Enum):
    QUALIFY = "qualify"
    DEFER = "defer"
    DISCARD = "discard"


class CivicEditorialExclusion(str, Enum):
    """Why screening refused. Closed, because an exclusion is recorded on the Candidate and read
    back by an Owner months later: free text would make that record unusable."""

    OPINION = "opinion"
    POLL = "poll"
    HORSERACE = "horserace"
    COMMENTARY = "commentary"
    PERSUASION = "persuasion"
    ANONYMOUS_SOURCING = "anonymous_sourcing"
    PERSONAL_MATTER = "personal_matter"
    SPECULATION = "speculation"
    NOT_VOTER_RELEVANT = "not_voter_relevant"
    NOT_DURABLE = "not_durable"


class CivicPolicyStatus(str, Enum):
    """Where the thing being described stands. Closed and destination-owned: PartyMap renders
    it, so a status it has no word for cannot be allowed to exist."""

    ANNOUNCED = "announced"
    PROPOSED = "proposed"
    INTRODUCED = "introduced"
    PASSED = "passed"
    ENACTED = "enacted"
    IN_FORCE = "in_force"
    WITHDRAWN = "withdrawn"
    REJECTED = "rejected"
    COMPLETED = "completed"
    CORRECTED = "corrected"
    RETRACTED = "retracted"


class CivicVerifierVerdict(str, Enum):
    PASS = "pass"
    REJECT = "reject"


class CivicVerifierIssueCode(str, Enum):
    UNSUPPORTED_CLAIM = "unsupported_claim"
    CITATION_DRIFT = "citation_drift"
    LOCALE_DRIFT = "locale_drift"
    NEUTRALITY = "neutrality"
    NUMERIC_INCONSISTENCY = "numeric_inconsistency"
    SOURCE_ROLE = "source_role"
    CLAIM_TYPE = "claim_type"
    UNSAFE_CONTENT = "unsafe_content"
    MISSING_LOCALE = "missing_locale"


@dataclass(frozen=True)
class CivicScreeningVerdict:
    """Editorial suitability only. There is deliberately no field here for record kind, category,
    subject or evidence strength: screening cannot change what the deterministic engine decided,
    and the way to guarantee that is to give it no vocabulary for saying so."""

    decision: CivicScreeningDecision
    editorial_exclusion: CivicEditorialExclusion | None
    reason: str

    def __post_init__(self) -> None:
        qualified = self.decision is CivicScreeningDecision.QUALIFY
        if qualified and self.editorial_exclusion is not None:
            raise StructuredResponseError(
                "a qualifying Civic screening verdict cannot name an editorial exclusion")
        if not qualified and self.editorial_exclusion is None:
            raise StructuredResponseError(
                f"a {self.decision.value} Civic screening verdict must name an editorial "
                "exclusion")

    @property
    def is_qualified(self) -> bool:
        return self.decision is CivicScreeningDecision.QUALIFY


@dataclass(frozen=True)
class CivicDraftClaim:
    """One factual sentence and the aliases that support it. A claim with no citation cannot be
    constructed, which is what makes 'every published claim is sourced' a type-level guarantee."""

    text: str
    citations: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise StructuredResponseError("a Civic draft claim needs text")
        if not self.citations:
            raise StructuredResponseError(f"Civic draft claim {self.text[:40]!r} cites nothing")
        if len(set(self.citations)) != len(self.citations):
            raise StructuredResponseError(
                f"Civic draft claim {self.text[:40]!r} cites a duplicate alias")


@dataclass(frozen=True)
class CivicLocaleContent:
    """One locale's finished entry. The same shape for the canonical base and every overlay, so
    a parity check compares like with like rather than two half-different structures."""

    locale: CivicLocale
    title: str
    summary: str
    event_date: date
    policy_status: CivicPolicyStatus
    uncertainty: bool
    entities: tuple[str, ...]
    claims: tuple[CivicDraftClaim, ...]

    def __post_init__(self) -> None:
        if not self.claims:
            raise StructuredResponseError(
                f"Civic {self.locale.value} content carries no claims")

    @property
    def citations(self) -> tuple[str, ...]:
        """Every alias this entry cites, deduplicated, in first-use order."""
        seen: list[str] = []
        for claim in self.claims:
            for alias in claim.citations:
                if alias not in seen:
                    seen.append(alias)
        return tuple(seen)

    @property
    def citation_shape(self) -> tuple[tuple[str, ...], ...]:
        """Per-claim citation sets, in claim order: the thing every locale must reproduce."""
        return tuple(tuple(sorted(claim.citations)) for claim in self.claims)


@dataclass(frozen=True)
class CivicGenerationResult:
    """One `generate` stage response: a screening outcome, and the four locales when it passed.

    Discriminated, and the discriminant is enforced by the type rather than by the caller
    remembering to check: a qualifying result always carries all four locales, and a deferring or
    discarding one always carries none. There is no half state where a Candidate was refused and
    still has an entry, or accepted and has nothing to publish."""

    screening: CivicScreeningVerdict
    contents: dict[CivicLocale, CivicLocaleContent] | None = None

    def __post_init__(self) -> None:
        if self.screening.is_qualified:
            if self.contents is None:
                raise StructuredResponseError(
                    "a qualifying Civic generation response must carry an entry")
            missing = [
                locale.value for locale in CivicLocale if locale not in self.contents]
            if missing:
                raise StructuredResponseError(
                    f"Civic generation response is missing locales {missing}; all four are "
                    "mandatory and there is no fallback")
        elif self.contents is not None:
            raise StructuredResponseError(
                f"a {self.screening.decision.value} Civic generation response cannot carry an "
                "entry")

    @property
    def is_qualified(self) -> bool:
        return self.screening.is_qualified

    @property
    def base(self) -> CivicLocaleContent:
        """The canonical Chinese entry every overlay is checked against."""
        if self.contents is None:
            raise StructuredResponseError("this Civic generation response carries no entry")
        return self.contents[CivicLocale.ZH]


@dataclass(frozen=True)
class CivicVerifierIssue:
    code: CivicVerifierIssueCode
    locale: CivicLocale | None
    detail: str


@dataclass(frozen=True)
class CivicVerifierReport:
    verdict: CivicVerifierVerdict
    issues: tuple[CivicVerifierIssue, ...]

    def __post_init__(self) -> None:
        if self.verdict is CivicVerifierVerdict.PASS and self.issues:
            raise StructuredResponseError(
                "a passing Civic verifier report cannot carry issues")
        if self.verdict is CivicVerifierVerdict.REJECT and not self.issues:
            raise StructuredResponseError(
                "a rejecting Civic verifier report must say what is wrong")

    @property
    def is_pass(self) -> bool:
        return self.verdict is CivicVerifierVerdict.PASS


_SCREENING_CONTEXT = "Civic generation"
_SCREENING_KEYS = frozenset({"decision", "editorial_exclusion", "reason", "entry"})
_CONTENT_KEYS = frozenset({
    "title", "summary", "event_date", "policy_status", "uncertainty", "entities", "claims"})
_CLAIM_KEYS = frozenset({"text", "citations"})
_VERIFIER_KEYS = frozenset({"verdict", "issues"})
_ISSUE_KEYS = frozenset({"code", "locale", "detail"})


def _closed(value: object, enum: type[Enum], *, field: str, context: str) -> Enum:
    if not isinstance(value, str):
        raise StructuredResponseError(f"{context} response '{field}' must be a string")
    try:
        return enum(value)
    except ValueError as exc:
        allowed = ", ".join(sorted(member.value for member in enum))
        raise StructuredResponseError(
            f"{context} response '{field}' is {value!r}, which is not one of: {allowed}"
        ) from exc


def parse_generation_response(raw_text: str) -> CivicGenerationResult:
    """The whole `generate` stage response: screening decision plus, when it qualifies, all four
    locales.

    Structure only. Alias existence, snapshot agreement and cross-locale parity are
    `candidate_policy`'s job, because those need the snapshot and this module deliberately knows
    nothing about it."""
    parsed = extract_fenced_json_object(raw_text, context=_SCREENING_CONTEXT)
    require_exact_keys(parsed, allowed_keys=_SCREENING_KEYS, context=_SCREENING_CONTEXT)
    missing = sorted(_SCREENING_KEYS - set(parsed))
    if missing:
        raise StructuredResponseError(
            f"{_SCREENING_CONTEXT} response is missing {missing}")
    decision = _closed(
        parsed["decision"], CivicScreeningDecision, field="decision", context=_SCREENING_CONTEXT)
    raw_exclusion = parsed["editorial_exclusion"]
    exclusion = None if raw_exclusion is None else _closed(
        raw_exclusion, CivicEditorialExclusion, field="editorial_exclusion",
        context=_SCREENING_CONTEXT)
    reason = require_string(
        parsed["reason"], field="reason", max_len=MAX_REASON_CHARS, context=_SCREENING_CONTEXT)
    verdict = CivicScreeningVerdict(
        decision=decision, editorial_exclusion=exclusion, reason=reason)

    raw_entry = parsed["entry"]
    if raw_entry is None:
        return CivicGenerationResult(screening=verdict, contents=None)
    entry = require_dict(raw_entry, field="entry", context=_SCREENING_CONTEXT)
    require_exact_keys(
        entry, allowed_keys=frozenset(locale.value for locale in CivicLocale),
        context=f"{_SCREENING_CONTEXT} entry")
    contents = {
        locale: parse_locale_content(entry[locale.value], locale)
        for locale in CivicLocale if locale.value in entry
    }
    return CivicGenerationResult(screening=verdict, contents=contents)


def parse_locale_content(raw: object, locale: CivicLocale) -> CivicLocaleContent:
    """One locale object out of the entry, structurally validated."""
    context = f"Civic {locale.value} content"
    parsed = require_dict(raw, field=locale.value, context=context)
    require_exact_keys(parsed, allowed_keys=_CONTENT_KEYS, context=context)
    missing = sorted(_CONTENT_KEYS - set(parsed))
    if missing:
        raise StructuredResponseError(f"{context} response is missing {missing}")

    title = require_string(
        parsed["title"], field="title", max_len=MAX_TITLE_CHARS + 1, context=context)
    if len(title) > MAX_TITLE_CHARS:
        raise StructuredResponseError(
            f"{context} title is longer than {MAX_TITLE_CHARS} characters")
    summary = require_string(
        parsed["summary"], field="summary", max_len=MAX_SUMMARY_CHARS + 1, context=context)
    if len(summary) > MAX_SUMMARY_CHARS:
        raise StructuredResponseError(
            f"{context} summary is longer than {MAX_SUMMARY_CHARS} characters")

    raw_date = require_string(
        parsed["event_date"], field="event_date", max_len=10, context=context)
    try:
        event_date = date.fromisoformat(raw_date)
    except ValueError as exc:
        raise StructuredResponseError(
            f"{context} event_date {raw_date!r} is not an ISO date") from exc

    status = _closed(
        parsed["policy_status"], CivicPolicyStatus, field="policy_status", context=context)
    uncertainty = require_bool(parsed["uncertainty"], field="uncertainty", context=context)

    raw_entities = require_list(parsed["entities"], field="entities", context=context)
    if len(raw_entities) > MAX_ENTITIES_PER_CANDIDATE:
        raise StructuredResponseError(
            f"{context} names {len(raw_entities)} entities, exceeding "
            f"{MAX_ENTITIES_PER_CANDIDATE}")
    entities: list[str] = []
    for entry in raw_entities:
        if not isinstance(entry, str) or not entry.strip():
            raise StructuredResponseError(f"{context} has a non-string or blank entity")
        entities.append(entry.strip())
    if len(set(entities)) != len(entities):
        raise StructuredResponseError(f"{context} names a duplicate entity")

    raw_claims = require_list(parsed["claims"], field="claims", context=context)
    if not raw_claims:
        raise StructuredResponseError(f"{context} carries no claims")
    if len(raw_claims) > MAX_CLAIMS_PER_CANDIDATE:
        raise StructuredResponseError(
            f"{context} carries {len(raw_claims)} claims, exceeding "
            f"{MAX_CLAIMS_PER_CANDIDATE}")
    claims = tuple(
        _parse_claim(entry, context=f"{context} claim {index}")
        for index, entry in enumerate(raw_claims, start=1))

    return CivicLocaleContent(
        locale=locale, title=title, summary=summary, event_date=event_date,
        policy_status=status, uncertainty=uncertainty, entities=tuple(entities), claims=claims)


def _parse_claim(entry: object, *, context: str) -> CivicDraftClaim:
    entry = require_dict(entry, field="claims", context=context)
    require_exact_keys(entry, allowed_keys=_CLAIM_KEYS, context=context)
    missing = sorted(_CLAIM_KEYS - set(entry))
    if missing:
        raise StructuredResponseError(f"{context} is missing {missing}")
    text = require_string(
        entry["text"], field="text", max_len=MAX_CLAIM_CHARS + 1, context=context)
    if len(text) > MAX_CLAIM_CHARS:
        raise StructuredResponseError(
            f"{context} text is longer than {MAX_CLAIM_CHARS} characters")
    raw_citations = require_list(entry["citations"], field="citations", context=context)
    if not raw_citations:
        raise StructuredResponseError(f"{context} cites nothing")
    if len(raw_citations) > MAX_CITATIONS_PER_CLAIM:
        raise StructuredResponseError(
            f"{context} cites {len(raw_citations)} aliases, exceeding "
            f"{MAX_CITATIONS_PER_CLAIM}")
    citations: list[str] = []
    for alias in raw_citations:
        if not isinstance(alias, str) or not alias.strip():
            raise StructuredResponseError(f"{context} has a non-string or blank citation")
        citations.append(alias.strip())
    return CivicDraftClaim(text=text, citations=tuple(citations))


def parse_verification_response(raw_text: str) -> CivicVerifierReport:
    context = "Civic verification"
    parsed = extract_fenced_json_object(raw_text, context=context)
    require_exact_keys(parsed, allowed_keys=_VERIFIER_KEYS, context=context)
    missing = sorted(_VERIFIER_KEYS - set(parsed))
    if missing:
        raise StructuredResponseError(f"{context} response is missing {missing}")
    verdict = _closed(
        parsed["verdict"], CivicVerifierVerdict, field="verdict", context=context)
    raw_issues = require_list(parsed["issues"], field="issues", context=context)
    if len(raw_issues) > MAX_VERIFIER_ISSUES:
        raise StructuredResponseError(
            f"{context} reports {len(raw_issues)} issues, exceeding {MAX_VERIFIER_ISSUES}")
    issues = tuple(
        _parse_issue(entry, context=f"{context} issue {index}")
        for index, entry in enumerate(raw_issues, start=1))
    return CivicVerifierReport(verdict=verdict, issues=issues)


def _parse_issue(entry: object, *, context: str) -> CivicVerifierIssue:
    entry = require_dict(entry, field="issues", context=context)
    require_exact_keys(entry, allowed_keys=_ISSUE_KEYS, context=context)
    missing = sorted(_ISSUE_KEYS - set(entry))
    if missing:
        raise StructuredResponseError(f"{context} is missing {missing}")
    code = _closed(entry["code"], CivicVerifierIssueCode, field="code", context=context)
    raw_locale = entry["locale"]
    locale = None if raw_locale is None else _closed(
        raw_locale, CivicLocale, field="locale", context=context)
    detail = require_string(
        entry["detail"], field="detail", max_len=MAX_REASON_CHARS, context=context)
    return CivicVerifierIssue(code=code, locale=locale, detail=detail)


__all__ = [
    "CivicDraftClaim",
    "CivicGenerationResult",
    "CivicEditorialExclusion",
    "CivicLocaleContent",
    "CivicPolicyStatus",
    "CivicScreeningDecision",
    "CivicScreeningVerdict",
    "CivicVerifierIssue",
    "CivicVerifierIssueCode",
    "CivicVerifierReport",
    "CivicVerifierVerdict",
    "StructuredResponseError",
    "parse_generation_response",
    "parse_locale_content",
    "parse_verification_response",
]
