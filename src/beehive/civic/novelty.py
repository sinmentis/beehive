"""Conservative identities for final civic events, not articles or broad political topics."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Mapping, Sequence

NOVELTY_WINDOW = timedelta(days=90)
MAX_NOVELTY_HISTORY = 1024

NOVELTY_RULES = """
- Preserve the source's actual speech act. An article date does not make an existing policy a
  new announcement. Distinguish a first announcement from reiteration, advocacy, criticism,
  reaction or background. "Our policy would" is not evidence that a policy was newly announced
  that day. If no durable new development is established, defer or discard.
- Describe one underlying development, not the fact that another spokesperson issued a release.
  Separate articles can report the same event; complementary details do not make two events.
  Conversely, a shared party, topic, URL or number alone does not prove an event is a duplicate.
- For legislation, preserve the formal bill identity and actual stage: introduction, first
  reading, second reading, final/third reading, Royal Assent and commencement are distinct.
  "Will take effect 28 days after Royal Assent" describes a condition, not assent or commencement
  already occurring. A new committee report, amendment, implementation step or material policy
  revision can be a genuine follow-up; do not collapse it into an earlier milestone.
- Do not invent novelty to justify publication. Verify every newness claim against the cited
  source, including titles and summaries, not merely the separate claims array.
"""

_FORMAL_BILL = re.compile(
    r"(?<!\w)((?:(?:[A-Z][\w'-]*|and|of|the|for|on|in|to)\s+"
    r"|\([^()\n]{1,140}\)\s+){2,20}Bill(?:\s*\(No\.?\s*\d+\))?(?:\s+\d{4})?)(?!\w)")
_BILL_GENERIC = frozenset({
    "a", "an", "the", "and", "of", "for", "on", "in", "to", "bill", "amendment",
})
_PASS = r"(?:pass(?:ed|es|ing)?|clear(?:ed|s)?|approv(?:ed|es|al)|complet(?:ed|es))"
_READING_GAP = r"(?:(?!\breading\b)[^.!?;]){0,100}"
_STAGES = (
    ("commencement", re.compile(r"\b(?:came|comes|entered|enters|brought|took|takes)\s+"
                               r"(?:into\s+(?:force|effect)|effect)\b", re.I)),
    ("assent", re.compile(r"\b(?:received?|receives|granted|gains?|given)\s+"
                         r"(?:the\s+)?royal\s+assent\b", re.I)),
    ("final-passage", re.compile(rf"\b{_PASS}\b{_READING_GAP}\b(?:third|final)\s+reading\b", re.I)),
    ("second-reading", re.compile(rf"\b{_PASS}\b{_READING_GAP}\bsecond\s+reading\b", re.I)),
    ("first-reading", re.compile(rf"\b{_PASS}\b{_READING_GAP}\bfirst\s+reading\b", re.I)),
    ("introduction", re.compile(
        r"\bbill\s+(?:was|is|has\s+been|had\s+been)\s+introduced\b|"
        r"\bintroduc(?:ed|es)\s+(?:the|a|this)\s+bill\b|"
        r"\bintroduction\s+of\s+(?:the\s+)?bill\b", re.I)),
)
_FUTURE = re.compile(
    r"\b(?:will|would|could|may|might|if|when|unless|once|until|awaits?|expected\s+to|due\s+to|"
    r"scheduled\s+to|plans?\s+to)\b", re.I)
_FUTURE_TAIL = re.compile(r"\b(?:tomorrow|next\s+(?:week|month|year)|later)\b", re.I)
_CONDITION_TAIL = re.compile(r"\b(?:after|before|when|if|unless|once|until)\b", re.I)
_PAST_EVENT = re.compile(r"\b(?:passed|cleared|approved|completed|received|granted|given|came|entered|took|brought|introduced)\b", re.I)
_FOLLOWUP_TITLE = re.compile(
    r"\b(?:committee\s+report|reported\s+back|consultation|submissions|reintroduc\w*|"
    r"new\s+amendments|revised\s+(?:text|bill)|implementation\s+guidance|"
    r"criticis\w*|criticiz\w*|respond\w*|reaction|defend\w*|clarif\w*|review\w*)\b", re.I)
_POLICY = re.compile(r"\b(?:policy|policies|proposal|propos\w*|plan|pledge)\b", re.I)
_POLICY_FOLLOWUP = re.compile(
    r"\b(?:revis\w*|updat\w*|amend\w*|extend\w*|implement\w*|rollout|consultation|"
    r"pilot|agreement|appointments?|appointed|signed)\b", re.I)
_QUANTITY = re.compile(r"(?<![\w./-])(\d+(?:,\d{3})*(?:\.\d+)?)\s+([a-z]{3,})\b", re.I)
_GENERAL_UNITS = frozenset({
    "day", "week", "month", "year", "million", "billion", "thousand", "percent", "people",
    "person", "dollar", "more", "less", "new", "additional", "policy", "proposal",
})
_COUNT_ENTITIES = frozenset({
    "minister", "department", "ministry", "agency", "school", "hospital", "clinic",
    "teacher", "nurse", "doctor", "officer", "home", "house", "apartment", "bed",
    "classroom", "train", "bus", "station", "vessel", "aircraft", "council",
})
_POLICY_WORDS = frozenset({
    "a", "an", "the", "and", "of", "for", "on", "in", "to", "with", "by", "at", "as",
    "is", "are", "was", "were", "be", "been", "have", "has", "had", "would", "will",
    "it", "its", "that", "this", "said", "say", "says", "party", "new", "zealand",
    "policy", "policie", "proposal", "propose", "proposed", "proposes", "plan", "pledge",
    "announced", "announcement", "announces", "current", "structure", "restructuring",
    "restructure", "smaller", "fewer", "reduce", "reduced", "reducing", "cut", "cuts",
    "lower", "lowering", "shrink", "increase", "increased", "increasing", "more",
})
_POLICY_ACTIONS = (
    ("resize-down", re.compile(r"\b(?:restructur\w*|smaller|fewer|reduc\w*|cuts?|lower\w*|shrink\w*)\b", re.I)),
    ("resize-up", re.compile(r"\b(?:increas\w*|expand\w*|additional|more)\b", re.I)),
    ("create", re.compile(r"\b(?:build\w*|construct\w*|creat\w*|establish\w*)\b", re.I)),
    ("fund", re.compile(r"\b(?:allocat\w*|invest\w*|fund\w*)\b", re.I)),
)
_SCOPE = re.compile(
    r"\b(?:by|until|before|over|for)\s+(\d+(?:-\d{2}-\d{2})?(?:\s+(?:years?|months?|weeks?))?)\b"
    r"|\b(per\s+(?:year|month|week)|annually|monthly|weekly)\b", re.I)
_PLACE = re.compile(r"\b(?:in|across|at|for)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})\b")
_REITERATION = re.compile(
    r"\b(?:(?:reiterat\w*|reaffirm\w*|restat\w*)[^.!?;]{0,60}"
    r"\b(?:policy|plan|proposal|position|pledge|commitment)|"
    r"repeat(?:ed|s|ing)?\s+(?:(?:its|the|our|their|existing|previous)\s+){0,3}"
    r"(?:policy|plan|proposal|position|pledge|call)|"
    r"(?:existing|long-standing|longstanding|previously announced)\s+(?:policy|plan|proposal)|"
    r"has\s+(?:long|already|consistently)\s+(?:advocated|proposed|called|argued)|"
    r"remains?\s+committed)\b", re.I)
_NEW_ANNOUNCEMENT = re.compile(
    r"\b(?:announc\w*|unveil\w*|launch\w*|introduc\w*)\b[^.!?]{0,90}"
    r"\b(?:policy|plan|proposal|pledge)\b", re.I)
_SOURCE_NEW_POLICY = re.compile(
    r"\b(?:announc\w*|unveil\w*|launch\w*|introduc\w*)\b[^.!?]{0,60}"
    r"\bnew\s+(?:policy|plan|proposal|pledge)\b", re.I)


@dataclass(frozen=True)
class CivicEventIdentity:
    subject_id: str
    event_date: date
    family: str
    entity: tuple[str, ...]
    stage: str
    anchors: frozenset[str] = frozenset()
    qualifiers: tuple[str, ...] = ()


@dataclass(frozen=True)
class CivicPriorEvent:
    candidate_id: int
    batch_id: int
    identity: CivicEventIdentity


def _words(text: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKD", text)
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    return tuple(re.findall(r"[a-z0-9]+", normalized.casefold()))


def _singular(word: str) -> str:
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word == "buses":
        return "bus"
    return word[:-1] if len(word) > 4 and word.endswith("s") and not word.endswith("ss") else word


def _bill_titles(text: str) -> tuple[str, ...]:
    found = []
    for match in _FORMAL_BILL.finditer(text):
        name = re.split(
            r"\b(?:welcomes|backs|supports|announces|reports|says|hails|approves)\s+",
            match.group(1), flags=re.I)[-1]
        name = re.sub(
            r"^(?:(?:since|after|before|while|when|once|because|although|under|today|yesterday)\s+"
            r"(?:the|a|an)\s+|(?:the|a|an)\s+)", "", name, flags=re.I)
        found.append(name)
    return tuple(found)


def _bill_names(text: str) -> set[tuple[str, ...]]:
    found = set()
    for name in _bill_titles(text):
        words = tuple(_singular(word) for word in _words(name)
                      if word not in _BILL_GENERIC)
        if len(set(words)) >= 3:
            found.add(words)
    return found


def _asserted_match(
    pattern: re.Pattern, text: str, *, event_date: str = "", present_needs_date: bool = False,
) -> bool:
    for match in pattern.finditer(text):
        before = re.split(r"(?<!No)[.!?;]", text[:match.start()], flags=re.I)[-1]
        if _FUTURE.search(before) or _FUTURE.search(match.group(0)):
            continue
        if re.search(r"\bbill\s*,?\s*(?:which|that)\s+(?:(?:had|has|was|been)\s+)*$", before, re.I):
            continue
        tail = re.split(r"[.!?;,]|\b(?:and|but|whereas|while)\b", text[match.end():], maxsplit=1,
                        flags=re.I)[0]
        if _FUTURE_TAIL.search(tail):
            continue
        if _CONDITION_TAIL.search(tail) and not _PAST_EVENT.search(match.group(0)):
            continue
        if event_date and any(day > event_date for day in re.findall(
                r"\b\d{4}-\d{2}-\d{2}\b", match.group(0) + tail)):
            continue
        if (present_needs_date and not _PAST_EVENT.search(match.group(0))
                and not re.search(r"\btoday\b", before + tail, re.I)
                and (not event_date or event_date not in before + tail)):
            continue
        return True
    return False


def _bill_stage(title: str, summary: str, event_date: str) -> str | None:
    if _FOLLOWUP_TITLE.search(title):
        return None
    for name in sorted(set(_bill_titles(f"{title}. {summary}")), key=len, reverse=True):
        title = title.replace(name, "bill")
        summary = summary.replace(name, "bill")
    for text in (title, summary):
        for stage, pattern in _STAGES:
            if _asserted_match(
                    pattern, text, event_date=event_date, present_needs_date=stage == "commencement"):
                return stage
        for ordinal, stage in (
            ("third|final", "final-passage"), ("second", "second-reading"), ("first", "first-reading"),
        ):
            if _asserted_match(re.compile(
                    rf"\b(?:gave|gives)\s+(?:the\s+)?bill\s+(?:its|a|the)\s+"
                    rf"(?:{ordinal})\s+reading\b", re.I), text, event_date=event_date):
                return stage
    generic_passage = re.compile(
        r"\b(?:bill|legislation|law)\s+(?:(?:had|has|was|is)\s+)?(?:been\s+)?pass(?:ed|es)\b"
        r"|\bpassed\s+(?:by\s+)?(?:Parliament|unanimously)\b"
        r"|\b(?:Parliament|the House|MPs)\s+(?:(?:has|had)\s+)?(?:unanimously\s+)?"
        r"(?:passed|passes|voted\s+to\s+pass)\s+(?:the\s+)?(?:bill|legislation)\b", re.I)
    if _asserted_match(generic_passage, f"{title}. {summary}", event_date=event_date):
        return "final-passage"
    return None


def _policy_event(item: Mapping) -> CivicEventIdentity | None:
    english = item["locales"]["en"]
    title, summary = english["title"], english["summary"]
    text = f"{title}. {summary}"
    if not _POLICY.search(text) or _POLICY_FOLLOWUP.search(text):
        return None
    subject = item["subject"]["id"]

    def facts(value: str) -> tuple[tuple[str, str], ...]:
        return tuple(sorted({
            (number.replace(",", ""), _singular(unit.casefold()))
            for number, unit in _QUANTITY.findall(value)
            if _singular(unit.casefold()) not in _GENERAL_UNITS
            and _singular(unit.casefold()) in _COUNT_ENTITIES
            and unit.casefold() not in _words(subject)
        }))

    quantities = facts(title)
    if len(quantities) < 2:
        quantities = facts(summary)
    if len({unit for _, unit in quantities}) < 2:
        return None
    actions = [name for name, pattern in _POLICY_ACTIONS if pattern.search(text)]
    if len(actions) != 1:
        return None
    units = {unit for _, unit in quantities}
    anchors = frozenset(
        _singular(word) for word in _words(text)
        if word.isalpha() and len(word) > 3 and word not in _words(subject)
        and _singular(word) not in units and _singular(word) not in _POLICY_WORDS
        and not any(pattern.fullmatch(word) for _, pattern in _POLICY_ACTIONS))
    if not anchors:
        return None
    scopes = {match.group(0).casefold() for match in _SCOPE.finditer(text)}
    scopes.update(f"place:{match.group(1).casefold()}" for match in _PLACE.finditer(text))
    return CivicEventIdentity(
        subject, date.fromisoformat(item["event_date"]), "policy",
        tuple(f"{number}:{unit}" for number, unit in quantities), actions[0],
        anchors, tuple(sorted(scopes)))


def source_newness_issue(
    *, title: str, summary: str, source_texts: Sequence[str], subject_id: str = "",
) -> str | None:
    """Reject explicit promotion of a stated old policy, not mere absence of the word 'new'."""
    output = f"{title}. {summary}"
    if not _NEW_ANNOUNCEMENT.search(output) or _REITERATION.search(output):
        return None
    ignored = _POLICY_WORDS - {
        "smaller", "fewer", "reduce", "reduced", "reducing", "restructuring", "restructure",
        "cut", "cuts", "lower", "lowering", "shrink", "increase", "increased", "increasing",
    }

    def subject_words(text: str) -> set[str]:
        return {_singular(word) for word in _words(text)
                if word.isalpha() and len(word) > 3 and _singular(word) not in ignored
                and word not in _words(subject_id)}

    output_words = subject_words(output)
    sentences = tuple(
        sentence for source in source_texts
        for sentence in re.split(r"(?<=[.!?;])\s+", source))
    if any(
        (_SOURCE_NEW_POLICY.search(sentence) or _NEW_ANNOUNCEMENT.search(sentence))
        and not _REITERATION.search(sentence)
        and len(output_words & subject_words(sentence)) >= 2
        for sentence in sentences
    ):
        return None
    for sentence in sentences:
        if (_REITERATION.search(sentence)
                and len(output_words & subject_words(sentence)) >= 2):
            return (
                "Cited material reiterates an existing position; "
                "the output invents a new policy announcement")
    return None


def identify_event(
    item: Mapping, *, source_texts: Sequence[str] = (),
) -> CivicEventIdentity | None:
    """Classify only sufficiently specific final events; unrecognized text is not a duplicate."""
    english = item["locales"]["en"]
    title, summary = english["title"], english["summary"]
    text = f"{title}. {summary}"
    stage = _bill_stage(title, summary, item["event_date"])
    if stage is None or not re.search(r"\b(?:bill|legislation|law)\b", text, re.I):
        return _policy_event(item)
    names = _bill_names(text)
    if not names:
        source_names = set().union(*(_bill_names(source) for source in source_texts))
        output_words = {_singular(word) for word in _words(text)}
        names = {name for name in source_names if len(set(name) & output_words) >= 2}
    if len(names) != 1:
        return _policy_event(item)
    return CivicEventIdentity(
        subject_id=item["subject"]["id"], event_date=date.fromisoformat(item["event_date"]),
        family="legislation", entity=next(iter(names)), stage=stage)


def find_duplicate_event(
    current: CivicEventIdentity | None, prior: Sequence[CivicPriorEvent],
) -> CivicPriorEvent | None:
    if current is None:
        return None
    for record in prior:
        earlier = record.identity
        if (current.subject_id == earlier.subject_id
                and current.family == earlier.family and current.entity == earlier.entity
                and current.stage == earlier.stage
                and abs(current.event_date - earlier.event_date) <= NOVELTY_WINDOW
                and current.qualifiers == earlier.qualifiers
                and (current.family != "policy" or bool(current.anchors & earlier.anchors))):
            return record
    return None
