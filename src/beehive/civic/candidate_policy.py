"""The deterministic checks every AI stage output must survive (plan 7.9).

The models produce text; this module enforces mechanical publication constraints and bounded
semantic regression fences. Everything here is pure and offline. A failed constraint refuses
the output; passing these checks does not prove every claim or translation correct. Independent
verification must still compare the actual meaning against the cited evidence.

What is checked, and why each one is here rather than trusted to a prompt:

* **Screening cannot promote.** `apply_screening_verdict` can only ever narrow the deterministic
  decision. A screening call that says "qualify" over a Candidate the evidence engine did not
  make publishable changes nothing, because eligibility is read from the recorded qualification,
  never from the model. The screening contract has no vocabulary for record kind, category or
  subject either, so there is nothing to reconcile.
* **Citations resolve to admissible, checkable evidence.** Aliases exist in the snapshot, and
  the snapshot only ever contains admissible, non-discovery, non-restricted evidence
  (`generation.is_citable`). An invented alias, an alias from another Candidate and a citation
  of excluded material are all the same failure here.
* **Nothing is minted.** No URL, no domain, no em dash, no en dash, no entity outside the
  snapshot's closed set, no number that is not already in the evidence. A model that cannot name
  a source it was not shown cannot fabricate one.
* **The four locales carry matching structure.** Same claim count and order, same per-claim citations,
  same entities, same policy status, same uncertainty, same event date, same numbers after
  Unicode digit normalization. Full dates compare as whole dates (a Chinese 2026年6月30日 is
  2026-06-30). Any difference is drift; equality is not proof of semantic parity.
* **Full dates are whole facts.** A full date in copy that its evidence states with its year
  (`calendar_text`) is supported as that date: "1 April 2027" supports 2027-04-01. Any other date
  still faces the digit check, so a recombined 2027-04-02 gains nothing.
* **All four locales exist.** A partially localized Candidate is rejected before verification is
  even attempted, so it can never be verified and can never be sealed.
* **A summary says more than its title.** `summary_lacks_substance` flags an English summary that
  only restates its title although the source says more. That is a weak draft, not a false
  statement, so the pipeline regenerates it in a later slot instead of rejecting the Candidate.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Callable, Mapping, Sequence

from beehive.civic.calendar_text import evidence_dates, full_dates, without_dates
from beehive.civic.candidate_parser import (CivicGenerationResult, CivicLocaleContent,
                                            CivicPolicyStatus, CivicScreeningDecision,
                                            CivicScreeningVerdict)
from beehive.civic.generation import CivicCandidateSnapshot, numeric_tokens
from beehive.civic.language_fidelity import language_fidelity_issues
from beehive.civic.limits import (BASE_LOCALE, MAX_CITATIONS_PER_CANDIDATE, REQUIRED_LOCALES,
                                  CivicLocale)
from beehive.db.civic_qualifications import CivicQualificationRecord

# Characters the destination dataset forbids in generated text (plan 7.9). Kept as literals
# rather than a unicodedata category so the rule reads exactly as the destination states it.
FORBIDDEN_DASHES = ("\u2014", "\u2013")

# A URL, a bare domain, or a protocol-relative link. The model is never shown a URL, so any of
# these means it invented one.
_URL_RE = re.compile(
    r"(?:[a-zA-Z][a-zA-Z0-9+.-]*://)|(?:\bwww\.)|(?:\b[a-z0-9-]+\.(?:com|net|org|nz|govt|co)\b)",
    re.IGNORECASE)

# Phrases that mark reporting this dataset never publishes. Deliberately short and specific:
# every entry names a sourcing or framing practice (plan 4.3 hard exclusions), not a topic, so a
# legitimate legislative summary cannot trip it. A false positive defers a Candidate, which is
# the direction this pipeline is designed to fail in.
_ANONYMOUS_SOURCE_MARKERS = (
    "sources said", "sources say", "unnamed source", "anonymous source", "speaking on condition",
    "declined to be named", "insiders said", "消息人士", "匿名消息", "不愿具名",
)
_OPINION_MARKERS = (
    "in my view", "we believe", "it is clear that", "critics argue", "supporters argue",
    "should be applauded", "should be condemned",
)
_AMBIGUOUS_FUNDING_PERIOD = re.compile(
    r"\b(?:\d+(?:\.\d+)?|[a-z]+)\s+(?:years?|months?)\s+of\s+"
    r"(?:NZ\$|\$|NZD\s+|USD\s+)\s*\d",
    re.IGNORECASE,
)
_MONEY_AMOUNT = re.compile(
    r"(?:NZ\$|\$|NZD\s+|USD\s+)\s*(?P<currency>\d+(?:\.\d+)?)"
    r"|(?P<unit>\d+(?:\.\d+)?)\s*(?:million|billion|thousand|dollars|m\b|bn\b|"
    r"百万|千万|亿|万|美元|मिलियन|बिलियन|डॉलर|milyon|bilyon)",
    re.IGNORECASE)
_RECURRING_MONEY_PERIODS = (
    re.compile(
        r"\b(?:per year|a year|each (?:financial )?year|every year|annually|yearly|"
        r"per annum|annual funding|kada taon|bawat taon|taun-taon)\b|"
        r"每年|年均|每一财年|प्रति वर्ष|हर साल|सालाना", re.IGNORECASE),
    re.compile(
        r"\b(?:per month|a month|each month|every month|monthly|kada buwan|bawat buwan)\b|"
        r"每月|月均|प्रति माह|हर महीने", re.IGNORECASE),
    re.compile(
        r"\b(?:per week|a week|each week|every week|weekly|kada linggo|bawat linggo)\b|"
        r"每周|周均|प्रति सप्ताह|हर हफ्ते", re.IGNORECASE),
)


def _recurring_amounts(text: str) -> set[tuple[str, int]]:
    """Conservative recurrence evidence, tied to the nearest amount in one sentence."""
    amounts = set()
    for sentence in re.split(r"[.!?。！？]\s+|\n", text):
        money = list(_MONEY_AMOUNT.finditer(sentence))
        for index, period in enumerate(_RECURRING_MONEY_PERIODS):
            for match in period.finditer(sentence):
                if not money:
                    continue
                nearest = min(money, key=lambda item: min(
                    abs(item.end() - match.start()), abs(match.end() - item.start())))
                distance = min(
                    abs(nearest.end() - match.start()), abs(match.end() - nearest.start()))
                if distance <= 48:
                    amounts.add((nearest.group("currency") or nearest.group("unit"), index))
    return amounts


class CivicContentIssue(str, Enum):
    """Why generated content was refused. Closed, because it is recorded as the Candidate's
    rejection code and read back by an Owner long after the run."""

    ALIAS_UNKNOWN = "alias_unknown"
    CITATION_NOT_ADMISSIBLE = "citation_not_admissible"
    TOO_MANY_CITATIONS = "too_many_citations"
    ENTITY_NOT_ALLOWED = "entity_not_allowed"
    NUMBER_NOT_IN_EVIDENCE = "number_not_in_evidence"
    EVENT_DATE_DRIFT = "event_date_drift"
    URL_MINTED = "url_minted"
    FORBIDDEN_DASH = "forbidden_dash"
    POLICY_STATUS_MISMATCH = "policy_status_mismatch"
    ANONYMOUS_SOURCING = "anonymous_sourcing"
    OPINION_CONTENT = "opinion_content"
    LOCALE_CLAIM_COUNT = "locale_claim_count"
    LOCALE_CITATION_DRIFT = "locale_citation_drift"
    LOCALE_ENTITY_DRIFT = "locale_entity_drift"
    LOCALE_STATUS_DRIFT = "locale_status_drift"
    LOCALE_UNCERTAINTY_DRIFT = "locale_uncertainty_drift"
    LOCALE_NUMBER_DRIFT = "locale_number_drift"
    LOCALE_DATE_DRIFT = "locale_date_drift"
    LOCALE_MISSING = "locale_missing"
    LOCALE_EMPTY_TEXT = "locale_empty_text"
    SCREENING_NOT_ELIGIBLE = "screening_not_eligible"
    UNCERTAINTY_UNSUPPORTED = "uncertainty_unsupported"
    QUANTITY_SCOPE_AMBIGUOUS = "quantity_scope_ambiguous"
    LOCALE_TERM_DRIFT = "locale_term_drift"


class CivicContentError(ValueError):
    """Generated content that may not be published. Carries the closed issue code so the caller
    can record why without parsing a message."""

    def __init__(self, issue: CivicContentIssue, detail: str) -> None:
        super().__init__(f"{issue.value}: {detail}")
        self.issue = issue
        self.detail = detail


@dataclass(frozen=True)
class CivicScreeningOutcome:
    """What screening did to a Candidate that the evidence engine already qualified."""

    accepted: bool
    verdict: CivicScreeningVerdict

    @property
    def rejection_code(self) -> str | None:
        if self.accepted:
            return None
        exclusion = self.verdict.editorial_exclusion
        return f"screening_{self.verdict.decision.value}_{exclusion.value}"


def apply_screening_verdict(
    qualification: CivicQualificationRecord, verdict: CivicScreeningVerdict,
) -> CivicScreeningOutcome:
    """Narrow, never widen.

    A screening verdict may hold back a Candidate the evidence engine qualified. It can never do
    the reverse: eligibility comes from `qualification`, and a "qualify" verdict over anything
    that is not already publishable raises rather than being honored, because a model that could
    overturn the evidence policy would make every deterministic guarantee in this package
    advisory."""
    if not qualification.is_publishable:
        raise CivicContentError(
            CivicContentIssue.SCREENING_NOT_ELIGIBLE,
            f"Civic Candidate {qualification.candidate_id} is {qualification.outcome.value}; "
            "screening cannot make it publishable")
    return CivicScreeningOutcome(
        accepted=verdict.decision is CivicScreeningDecision.QUALIFY, verdict=verdict)


def check_locale_content(
    content: CivicLocaleContent, snapshot: CivicCandidateSnapshot,
) -> None:
    """Every check one locale can be judged on alone, against the immutable snapshot."""
    label = f"Civic Candidate {snapshot.candidate_id} {content.locale.value}"
    aliases = snapshot.alias_map

    for claim in content.claims:
        for alias in claim.citations:
            if alias not in aliases:
                raise CivicContentError(
                    CivicContentIssue.ALIAS_UNKNOWN,
                    f"{label} cites {alias!r}, which is not an evidence alias for this "
                    f"candidate")
    citations = content.citations
    if len(citations) > MAX_CITATIONS_PER_CANDIDATE:
        raise CivicContentError(
            CivicContentIssue.TOO_MANY_CITATIONS,
            f"{label} cites {len(citations)} sources, exceeding "
            f"{MAX_CITATIONS_PER_CANDIDATE}")

    for entity in content.entities:
        if entity not in snapshot.allowed_entities:
            raise CivicContentError(
                CivicContentIssue.ENTITY_NOT_ALLOWED,
                f"{label} names {entity!r}, which is not one of this candidate's entities")

    if content.event_date != snapshot.event_date:
        raise CivicContentError(
            CivicContentIssue.EVENT_DATE_DRIFT,
            f"{label} dates the event {content.event_date.isoformat()}, not "
            f"{snapshot.event_date.isoformat()}")

    if not any(aliases[alias].event_date == content.event_date for alias in citations):
        raise CivicContentError(
            CivicContentIssue.EVENT_DATE_DRIFT,
            f"{label} does not cite the evidence establishing its event date")

    expected_status = _required_status(snapshot)
    if expected_status is not None and content.policy_status is not expected_status:
        raise CivicContentError(
            CivicContentIssue.POLICY_STATUS_MISMATCH,
            f"{label} reports policy status {content.policy_status.value}, but a "
            f"{snapshot.record_kind} record must be {expected_status.value}")

    outcome_statuses = {
        CivicPolicyStatus.PASSED, CivicPolicyStatus.ENACTED,
        CivicPolicyStatus.IN_FORCE, CivicPolicyStatus.COMPLETED,
    }
    if (content.policy_status in outcome_statuses
            and all(aliases[key].source_kind == "party-primary" for key in citations)):
        raise CivicContentError(
            CivicContentIssue.POLICY_STATUS_MISMATCH,
            f"{label} presents a party-source-only statement as a confirmed outcome")

    text = _all_text(content)
    if not text.strip():
        raise CivicContentError(
            CivicContentIssue.LOCALE_EMPTY_TEXT, f"{label} has no text")
    if content.locale is CivicLocale.EN and _AMBIGUOUS_FUNDING_PERIOD.search(text):
        raise CivicContentError(
            CivicContentIssue.QUANTITY_SCOPE_AMBIGUOUS,
            f"{label} must distinguish a period total from a recurring amount")
    cited_text = " ".join(
        f"{aliases[key].title} {aliases[key].excerpt}" for key in citations)
    if (content.locale is CivicLocale.ZH
            and re.search(r"\bMainland\b", cited_text, re.IGNORECASE)
            and re.search(r"大陆|內地|内地", text)):
        raise CivicContentError(
            CivicContentIssue.LOCALE_TERM_DRIFT,
            f"{label} changes the New Zealand campaign location")
    if _URL_RE.search(text):
        raise CivicContentError(
            CivicContentIssue.URL_MINTED,
            f"{label} contains a link or domain; generated text may never mint one")
    for dash in FORBIDDEN_DASHES:
        if dash in text:
            raise CivicContentError(
                CivicContentIssue.FORBIDDEN_DASH,
                f"{label} contains a forbidden dash character")
    lowered = text.lower()
    for marker in _ANONYMOUS_SOURCE_MARKERS:
        if marker in lowered:
            raise CivicContentError(
                CivicContentIssue.ANONYMOUS_SOURCING,
                f"{label} attributes a fact to an unnamed source")
    for marker in _OPINION_MARKERS:
        if marker in lowered:
            raise CivicContentError(
                CivicContentIssue.OPINION_CONTENT,
                f"{label} reads as commentary rather than civic reference copy")

    stated_dates = evidence_dates(
        (part for entry in snapshot.aliases for part in (entry.title, entry.excerpt)),
        event_dates=(entry.event_date for entry in snapshot.aliases))
    allowed_numbers = set(snapshot.numeric_tokens)
    for token in numeric_tokens(without_dates(text, stated_dates)):
        if token not in allowed_numbers:
            raise CivicContentError(
                CivicContentIssue.NUMBER_NOT_IN_EVIDENCE,
                f"{label} states the number {token}, which is not in the evidence")

    def require_cited_content(value: str, cited: tuple[str, ...]) -> None:
        source_text = "\n".join(
            f"{aliases[key].title}\n{aliases[key].excerpt}\n"
            f"{aliases[key].event_date.isoformat()}"
            for key in cited
        )
        supported = set(numeric_tokens(source_text, include_english_words=True))
        stated_dates = evidence_dates(
            (part for key in cited for part in (aliases[key].title, aliases[key].excerpt)),
            event_dates=(aliases[key].event_date for key in cited))
        if any(number not in supported
               for number in numeric_tokens(without_dates(value, stated_dates))):
            raise CivicContentError(
                CivicContentIssue.NUMBER_NOT_IN_EVIDENCE,
                f"{label} contains a number absent from its cited evidence")
        if not _recurring_amounts(value) <= _recurring_amounts(source_text):
            raise CivicContentError(
                CivicContentIssue.QUANTITY_SCOPE_AMBIGUOUS,
                f"{label} introduces a recurring funding amount absent from its citation")
        try:
            reference_date = max(
                date.fromisoformat(aliases[key].published_at)
                if aliases[key].published_at != "unknown" else aliases[key].event_date
                for key in cited)
        except ValueError as exc:
            raise CivicContentError(
                CivicContentIssue.LOCALE_DATE_DRIFT,
                f"{label} has invalid citation publication-date provenance") from exc
        for issue in language_fidelity_issues(
            locale=content.locale, text=value, cited_text=source_text,
            reference_date=reference_date,
        ):
            raise CivicContentError(
                CivicContentIssue.LOCALE_DATE_DRIFT if issue.kind == "date"
                else CivicContentIssue.LOCALE_TERM_DRIFT,
                f"{label}: {issue.detail}")

    require_cited_content(f"{content.title}\n{content.summary}", citations)
    for claim in content.claims:
        require_cited_content(claim.text, claim.citations)


def _required_status(snapshot: CivicCandidateSnapshot) -> CivicPolicyStatus | None:
    """Corrections and retractions are decided before drafting, so their status is not the
    model's to choose."""
    if snapshot.operation == "correct":
        return CivicPolicyStatus.CORRECTED
    if snapshot.operation == "retract":
        return CivicPolicyStatus.RETRACTED
    return None


def _all_text(content: CivicLocaleContent) -> str:
    return "\n".join([content.title, content.summary, *(claim.text for claim in content.claims)])


def check_locale_parity(
    base: CivicLocaleContent, overlay: CivicLocaleContent,
    *, stated_dates_for: Callable[[Sequence[str]], frozenset[date]] | None = None,
) -> None:
    """Check an overlay's mechanical alignment with the base, not full semantic equivalence.

    A full date that a text's own citations state (`stated_dates_for`) compares as that date in
    whichever notation each locale writes it; any other date compares digit by digit."""
    label = f"locale {overlay.locale.value}"
    if len(overlay.claims) != len(base.claims):
        raise CivicContentError(
            CivicContentIssue.LOCALE_CLAIM_COUNT,
            f"{label} carries {len(overlay.claims)} claims, not the base's {len(base.claims)}")
    if overlay.citation_shape != base.citation_shape:
        raise CivicContentError(
            CivicContentIssue.LOCALE_CITATION_DRIFT,
            f"{label} changes which sources support which claim")
    if sorted(overlay.entities) != sorted(base.entities):
        raise CivicContentError(
            CivicContentIssue.LOCALE_ENTITY_DRIFT,
            f"{label} names {sorted(overlay.entities)}, not {sorted(base.entities)}")
    if overlay.policy_status is not base.policy_status:
        raise CivicContentError(
            CivicContentIssue.LOCALE_STATUS_DRIFT,
            f"{label} reports {overlay.policy_status.value}, not "
            f"{base.policy_status.value}")
    if overlay.uncertainty != base.uncertainty:
        raise CivicContentError(
            CivicContentIssue.LOCALE_UNCERTAINTY_DRIFT,
            f"{label} drops or adds the base's caveat")
    if overlay.event_date != base.event_date:
        raise CivicContentError(
            CivicContentIssue.LOCALE_DATE_DRIFT,
            f"{label} dates the event {overlay.event_date.isoformat()}, not "
            f"{base.event_date.isoformat()}")
    if (_parity_numbers(overlay, stated_dates_for)
            != _parity_numbers(base, stated_dates_for)):
        raise CivicContentError(
            CivicContentIssue.LOCALE_NUMBER_DRIFT,
            f"{label} does not contain exactly the base's numbers")



def _parity_numbers(
    content: CivicLocaleContent,
    stated_dates_for: Callable[[Sequence[str]], frozenset[date]] | None,
) -> tuple[list[date], tuple[str, ...]]:
    """A locale's numbers for parity. The title and summary use every citation of the entry and
    each claim its own, as the evidence checks do; a date those citations state is read as a
    date, any other stays as digits."""
    segments = [(f"{content.title}\n{content.summary}", content.citations)]
    segments.extend((claim.text, claim.citations) for claim in content.claims)
    dates: list[date] = []
    remainders: list[str] = []
    for text, cited in segments:
        stated = stated_dates_for(cited) if stated_dates_for is not None else frozenset()
        dates.extend(value for value in full_dates(text) if value in stated)
        remainders.append(without_dates(text, stated))
    return sorted(dates), numeric_tokens("\n".join(remainders))


def require_complete_localization(
    contents: Mapping[CivicLocale, CivicLocaleContent],
) -> tuple[CivicLocaleContent, ...]:
    """All four locales, or nothing. Returns them in the fixed locale order."""
    missing = [locale.value for locale in REQUIRED_LOCALES if locale not in contents]
    if missing:
        raise CivicContentError(
            CivicContentIssue.LOCALE_MISSING,
            f"missing locales {missing}; all four are mandatory and there is no fallback")
    return tuple(contents[locale] for locale in REQUIRED_LOCALES)


def check_generation_result(
    snapshot: CivicCandidateSnapshot,
    qualification: CivicQualificationRecord,
    result: CivicGenerationResult,
) -> CivicScreeningOutcome:
    """The whole deterministic gate over one `generate` response.

    Screening first, because a refused Candidate has no entry to check and must not be argued
    with; then, for one that qualified, every locale and every parity rule. Returns the screening
    outcome so the caller can settle a refusal without re-deriving why."""
    outcome = apply_screening_verdict(qualification, result.screening)
    if outcome.accepted:
        check_candidate_content(snapshot, result.contents or {})
    return outcome


def check_candidate_content(
    snapshot: CivicCandidateSnapshot, contents: Mapping[CivicLocale, CivicLocaleContent],
) -> tuple[CivicLocaleContent, ...]:
    """The whole deterministic gate: completeness, then each locale, then parity with the base.

    Called once, immediately before the independent verifier, so the verifier is never asked to
    approve something a mechanical check would already have refused."""
    ordered = require_complete_localization(contents)
    base = contents[BASE_LOCALE]
    for content in ordered:
        check_locale_content(content, snapshot)
    stated_by_alias = {
        name: evidence_dates((entry.title, entry.excerpt), event_dates=(entry.event_date,))
        for name, entry in snapshot.alias_map.items()
    }

    def stated_dates_for(cited: Sequence[str]) -> frozenset[date]:
        return frozenset().union(*(stated_by_alias.get(name, frozenset()) for name in cited))

    for content in ordered:
        if content.locale is not BASE_LOCALE:
            check_locale_parity(base, content, stated_dates_for=stated_dates_for)
    english_title = contents[CivicLocale.EN].title
    chinese_title = contents[CivicLocale.ZH].title
    if (re.search(r"\b(?:plans? to|proposes? to|proposal|planned|pledges? to|"
                  r"promises? to|intends? to|intention to|would|commits? to|set to)\b",
                  english_title, re.IGNORECASE)
            and re.search(r"废除|取消|实施|通过|削减|增税|减税", chinese_title)
            and not re.search(r"拟|将|计划|提议|建议|承诺|考虑", chinese_title)):
        raise CivicContentError(
            CivicContentIssue.LOCALE_STATUS_DRIFT,
            "Chinese action title omits the proposal qualifier in the English title")
    if base.uncertainty:
        raise CivicContentError(
            CivicContentIssue.UNCERTAINTY_UNSUPPORTED,
            f"Civic Candidate {snapshot.candidate_id} carries uncertainty that publication "
            "v1 cannot render")
    return ordered


# A summary exists to tell a reader what its title does not. Fewer new content terms than this
# (a measure, a place, a name, an amount) is a restatement of the title.
MIN_SUMMARY_NEW_TERMS = 3

# Below this much source text the source may hold nothing beyond its own headline, so a
# restating summary can be the most the evidence supports.
MIN_SUBSTANTIVE_EVIDENCE_CHARS = 400

_SUMMARY_WORD = re.compile(r"[^\W\d_]+(?:['\u2019][^\W\d_]+)?")
_SUMMARY_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
_SUMMARY_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b(?:19|20)\d{2}\b")

# Words that frame a summary without adding to it: function words, reporting verbs, document
# types, roles and honorifics, calendar words and the country's own name.
_SUMMARY_FILLER = frozenset("""
a an the and or but nor of to in on at for from by with as into onto about over under after
before than then that this these those which who whom whose what when where while it its itself
they them their he she his her him we our us you your i me my is are was were be been being has have
had will would should could can may might must shall do does did not no also so such very just
only both each any all some other another there here if because according via whether how
why up out off down again
say said says saying state stated states tell told announce announced announces announcing
announcement release released releases unveil unveiled unveils launch launched launches
publish published publishes outline outlined outlines reveal revealed reveals share shared
shares issue issued issues present presented presenting presents describe described explain
explained note noted confirm confirmed set sets statement statements media press speech
update updates policy policies plan plans planned proposal proposals programme programmes
program agenda initiative campaign party parties government first new zealand nz aotearoa
leader co deputy prime minister spokesperson mp mps mr mrs ms dr
today yesterday week weeks month months year years day days monday tuesday wednesday thursday
friday saturday sunday january february march april june july august september october
november december
""".split())


def _summary_stem(word: str) -> str:
    word = re.sub(r"['\u2019]s$", "", word)
    for suffix, replacement in (("ies", "y"), ("ing", ""), ("ed", ""), ("es", ""), ("s", "")):
        if (word.endswith(suffix) and len(word) - len(suffix) >= 3
                and not (suffix == "s" and word.endswith("ss"))):
            word = word[: len(word) - len(suffix)] + replacement
            break
    return word[:-1] if word.endswith("e") and len(word) > 3 else word


_SUMMARY_FILLER_STEMS = frozenset(_summary_stem(word) for word in _SUMMARY_FILLER)


def _summary_terms(text: str) -> set[str]:
    lowered = _SUMMARY_DATE.sub(" ", text.casefold())
    words = {
        _summary_stem(word) for word in _SUMMARY_WORD.findall(lowered)
        if len(word) > 1 and word not in _SUMMARY_FILLER
    }
    numbers = {number.replace(",", "") for number in _SUMMARY_NUMBER.findall(lowered)}
    return (words - _SUMMARY_FILLER_STEMS) | numbers


def summary_new_terms(title: str, summary: str) -> frozenset[str]:
    """The content terms an English summary adds beyond its title; dates and framing excluded."""
    return frozenset(_summary_terms(summary) - _summary_terms(title))


def summary_lacks_substance(
    snapshot: CivicCandidateSnapshot, content: CivicLocaleContent,
) -> bool:
    """True when the entry's summary restates its title although the source says more.

    Not a content error: the statement may be accurate, and the Candidate deserves a fresh
    attempt rather than a rejection."""
    if max((len(entry.excerpt) for entry in snapshot.aliases), default=0) < (
            MIN_SUBSTANTIVE_EVIDENCE_CHARS):
        return False
    return len(summary_new_terms(content.title, content.summary)) < MIN_SUMMARY_NEW_TERMS


def citation_evidence_ids(
    snapshot: CivicCandidateSnapshot, content: CivicLocaleContent,
) -> tuple[str, ...]:
    """The immutable evidence ids behind one entry's aliases, in citation order.

    This is the only path from generated text to a stored source: the alias is resolved here,
    against the snapshot, and the URL the reader eventually follows comes from the evidence row,
    never from anything a model wrote."""
    aliases = snapshot.alias_map
    resolved: list[str] = []
    for alias in content.citations:
        entry = aliases.get(alias)
        if entry is None:
            raise CivicContentError(
                CivicContentIssue.CITATION_NOT_ADMISSIBLE,
                f"alias {alias!r} does not resolve to admissible evidence")
        resolved.append(entry.evidence_id)
    return tuple(resolved)


def unsupported_locales(issues: Sequence[object]) -> tuple[str, ...]:
    """The locales a verifier report blamed, for the rejection record."""
    found: list[str] = []
    for issue in issues:
        locale = getattr(issue, "locale", None)
        if locale is not None and locale.value not in found:
            found.append(locale.value)
    return tuple(found)


__all__ = [
    "CivicContentError",
    "CivicContentIssue",
    "CivicScreeningOutcome",
    "FORBIDDEN_DASHES",
    "MIN_SUBSTANTIVE_EVIDENCE_CHARS",
    "MIN_SUMMARY_NEW_TERMS",
    "apply_screening_verdict",
    "check_candidate_content",
    "check_generation_result",
    "check_locale_content",
    "check_locale_parity",
    "citation_evidence_ids",
    "require_complete_localization",
    "summary_lacks_substance",
    "summary_new_terms",
    "unsupported_locales",
]
