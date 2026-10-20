"""Source-bound language regression fences, not a general translation validator."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import Literal

from beehive.civic.limits import CivicLocale


@dataclass(frozen=True)
class LanguageFidelityIssue:
    kind: Literal["term", "date"]
    detail: str


@dataclass(frozen=True)
class _TermRule:
    concept: str
    locale: CivicLocale
    source: re.Pattern[str]
    ambiguous_translation: re.Pattern[str]
    guidance: str
    source_exemption: re.Pattern[str] | None = None
    target_exemption: re.Pattern[str] | None = None


_TERM_RULES = (
    _TermRule(
        concept="medical_consultation",
        locale=CivicLocale.HI,
        source=re.compile(
            r"\b(?:GP|general practitioner|family doctor)\s+(?:visits?|appointments?|"
            r"consultations?)\b|\bvisits?\s+to\s+(?:a\s+)?GP\b", re.IGNORECASE),
        ambiguous_translation=re.compile(
            r"(?:जीपी|जी\s+पी|\bGP)\s+(?:(?:की|के|का)\s+)?(?:\d+\s+)?"
            r"(?:(?:मुफ्त|निःशुल्क)\s+)?यात्र", re.IGNORECASE),
        guidance=(
            "GP visits are medical consultations, not journeys or transport. In Hindi use "
            "जीपी से परामर्श or a similarly unambiguous clinical term, not जीपी यात्राओं."),
        source_exemption=re.compile(
            r"\b(?:transport|travel|mileage|fares?)\b[^.!?\n]{0,60}\bGP\b|"
            r"\bGP\b[^.!?\n]{0,60}\b(?:transport|travel|mileage|fares?)\b",
            re.IGNORECASE),
        target_exemption=re.compile(r"परिवहन|किराया|यातायात"),
    ),
    _TermRule(
        concept="fiscal_accounts",
        locale=CivicLocale.TL,
        source=re.compile(
            r"\breturn(?:ing|ed)?\s+(?:the\s+)?books\s+to\s+(?:a\s+)?surplus\b|"
            r"\bbalanc(?:e|ing)\s+(?:the\s+)?books\b|"
            r"\b(?:fiscal|financial|government|public)\s+accounts\b", re.IGNORECASE),
        ambiguous_translation=re.compile(
            r"\b(?:aklat|libro)\s+(?:sa|(?:pa)?tungo\s+sa)\s+surplus\b", re.IGNORECASE),
        guidance=(
            "Fiscal 'books' means government accounts or the fiscal balance, not reading "
            "material. In Filipino use an explicit financial expression such as mga account "
            "ng pamahalaan, not unqualified mga aklat sa surplus. Legitimate qualified "
            "accounting ledgers and physical library books are different senses."),
    ),
    _TermRule(
        concept="dental_sedation",
        locale=CivicLocale.HI,
        source=re.compile(
            r"\bsedation\s+(?:services?|pilots?|programmes?|programs?)\b", re.IGNORECASE),
        ambiguous_translation=re.compile(
            r"(?:दंत|डेंटल)\s+बेहोशी\s+(?:की\s+)?(?:सेवा|योजना|कार्यक्रम|पायलट)"),
        guidance=(
            "Preserve sedation versus general anaesthesia where the source distinguishes "
            "them. In Hindi prefer explicit सिडेशन for a sedation service; do not label it "
            "only दंत बेहोशी सेवा. A source actually about general anaesthesia may name it."),
    ),
)

_TL_PAST_DATE = re.compile(
    r"\bnoong\s+(?:taong\s+)?(?P<year>\d{4})"
    r"(?:-(?P<month>\d{2})-(?P<day>\d{2}))?(?!\d)", re.IGNORECASE)
_TL_MONTHS = {
    "enero": 1, "pebrero": 2, "marso": 3, "abril": 4, "mayo": 5, "hunyo": 6,
    "hulyo": 7, "agosto": 8, "setyembre": 9, "septiyembre": 9, "oktubre": 10,
    "nobyembre": 11, "disyembre": 12,
}
_TL_PAST_NAMED_DATE = re.compile(
    r"\bnoong\s+(?:(?:ika-?)?(?P<day_before>\d{1,2})(?:\s+ng)?\s+)?"
    r"(?P<month_name>" + "|".join(_TL_MONTHS) + r"),?\s+"
    r"(?:(?P<day_after>\d{1,2}),?\s+)?(?P<year>\d{4})(?!\d)", re.IGNORECASE)

LANGUAGE_FIDELITY_RULES = (
    "=== TARGET-LANGUAGE MEANING (applies to visible titles, summaries and every claim) ===\n"
    "Compare what each locale actually promises or reports against its own cited passages, "
    "not merely matching numbers, entity labels or hidden policy_status fields. Identify "
    "the actor, action, object, certainty, time and scope in each translation. Preserve "
    "future plans as future, retrospective events as past, and any conditions. If a domain "
    "term has no confidently equivalent translation, retain its precise source term with "
    "a short explanation rather than substitute a different benefit, treatment or concept.\n"
    + "\n".join(rule.guidance for rule in _TERM_RULES)
    + "\nFilipino noong marks a past time. Do not use it for a year or date later than the "
    "dated statement, such as planned Forum hosting in 2027 described in a 2026 release; "
    "use sa or an appropriate prospective construction. A past noong attached to the "
    "announcement date must not make a later planned event past.\n"
    "Use the supplied party identity consistently; spelling variants do not change the "
    "country or actor. Do not replace a party with a similarly named foreign organisation."
)


def language_fidelity_issues(
    *,
    locale: CivicLocale,
    text: str,
    cited_text: str,
    reference_date: date,
) -> tuple[LanguageFidelityIssue, ...]:
    text = unicodedata.normalize("NFC", text)
    cited_text = unicodedata.normalize("NFC", cited_text)
    boundaries = r"\n|(?<=[.!?;])\s+|(?<=[।。！？])\s*"
    target_sentences = re.split(boundaries, text)
    source_sentences = re.split(boundaries, cited_text)
    issues = [
        LanguageFidelityIssue(
            "term", f"{rule.concept}: target wording changes or obscures the cited concept")
        for rule in _TERM_RULES
        for sentence in target_sentences
        if locale is rule.locale
        and rule.source.search(cited_text)
        and rule.ambiguous_translation.search(sentence)
        and not (
            rule.source_exemption
            and any(rule.source_exemption.search(part) for part in source_sentences)
            and rule.target_exemption and rule.target_exemption.search(sentence))
    ]
    if locale is CivicLocale.TL:
        for pattern in (_TL_PAST_DATE, _TL_PAST_NAMED_DATE):
            for match in pattern.finditer(text):
                parts = match.groupdict()
                year = int(parts["year"])
                named_month = parts.get("month_name")
                month = (
                    _TL_MONTHS[named_month.casefold()] if named_month
                    else int(parts.get("month") or 1))
                day = int(
                    parts.get("day") or parts.get("day_before") or parts.get("day_after") or 1)
                try:
                    stated_date = date(year, month, day)
                except ValueError:
                    issues.append(LanguageFidelityIssue(
                        "date", "Filipino past-time phrase contains an invalid calendar date"))
                    continue
                if stated_date > reference_date:
                    issues.append(LanguageFidelityIssue(
                        "date", "Filipino past-time marker refers to a date after its cited report"))
    return tuple(issues)
