"""Adapter between the approved Civic evidence fixture corpus and the production engine.

`tests/civic/test_evidence_policy_fixtures.py` validates the corpus with the standard library
only, on purpose: fixtures that only validate against the code they constrain prove nothing.
This module is the other half of that arrangement. It maps each approved case onto the real
`beehive.civic` types and runs the real engine, so the production tests never restate an
expectation -- every number they assert is read out of the corpus.

**Every field is compared exactly.**

Disposition, outcome, reason codes, cited evidence, excluded evidence, ownership groups,
ownership clusters, dedup behaviour, retry bounds and batch caps are all asserted for exact
equality. `expected.reasonCodes` is the *complete* set of codes the engine must emit for a case,
not a sample of the codes the case was written to demonstrate, so an unrelated extra code is a
test failure rather than a tolerated addition.

That is only checkable because the corpus is internally consistent: two cases whose
policy-relevant inputs are identical record identical code sets. `policy_signature` is what makes
that claim testable rather than assumed, and `test_evidence_policy_engine.py` uses it to hold the
known input-identical pairs to the same expectation.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from beehive.civic.batch_policy import CivicRankedCandidate, CivicRunMode
from beehive.civic.dedup import (CivicDedupContext, PublishedFingerprintRecord,
                                 SealedCandidateRecord)
from beehive.civic.evidence import (HARD_EXCLUDED_CATEGORIES, CivicCandidateClaim,
                                    CivicClaimCategory, CivicContentClass, CivicEvidenceItem,
                                    CivicOwnershipRegistry, CivicPublicationDisposition,
                                    CivicPublisher, CivicPublisherKind, CivicQualification,
                                    CivicQualificationOutcome, CivicRecordKind,
                                    CivicRecordOperation, CivicResearchQualification,
                                    CivicSourceKind, CivicSourceTier)
from beehive.civic.reasons import CivicPolicyReason, CivicReasonDisposition, reason_disposition
from beehive.domain.civic import CivicEvidenceStance, CivicEvidenceTier

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "civic" / "evidence_policy"

SCHEMA: dict[str, Any] = json.loads((FIXTURE_ROOT / "schema.json").read_text(encoding="utf-8"))
REGISTRY_DOC: dict[str, Any] = json.loads(
    (FIXTURE_ROOT / "publishers.json").read_text(encoding="utf-8"))

# The fixture eligibility vocabulary against the engine's disposition. `deferred_unsupported` is
# an outcome in the corpus and a disposition in the engine, so the pair is matched, not the
# eligibility alone.
_ELIGIBILITY_BY_DISPOSITION: dict[CivicPublicationDisposition, str] = {
    CivicPublicationDisposition.PUBLISHABLE: "publishable",
    CivicPublicationDisposition.DEFERRED: "deferred",
    CivicPublicationDisposition.DEFERRED_UNSUPPORTED: "deferred",
    CivicPublicationDisposition.EXCLUDED: "blocked",
}

_DISPOSITION_CLASS: dict[CivicPublicationDisposition, CivicReasonDisposition] = {
    CivicPublicationDisposition.PUBLISHABLE: CivicReasonDisposition.ALLOW,
    CivicPublicationDisposition.DEFERRED: CivicReasonDisposition.DEFER,
    CivicPublicationDisposition.DEFERRED_UNSUPPORTED: CivicReasonDisposition.DEFER,
    CivicPublicationDisposition.EXCLUDED: CivicReasonDisposition.BLOCK,
}


def load_cases(kind: str) -> list[dict[str, Any]]:
    """Every approved case of one kind, in the corpus's own deterministic file order."""
    directory = SCHEMA["documents"][kind]["directory"]
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((FIXTURE_ROOT / directory).glob("*.json"))]


EVIDENCE_CASES: list[dict[str, Any]] = load_cases("evidence_case")
RUN_CASES: list[dict[str, Any]] = load_cases("run_case")


def build_registry() -> CivicOwnershipRegistry:
    """The corpus publisher registry as the production ownership registry."""
    return CivicOwnershipRegistry.build(
        CivicPublisher(
            publisher_id=entry["publisherId"],
            name=entry["name"],
            kind=CivicPublisherKind(entry["kind"]),
            source_kind=(
                CivicSourceKind(entry["sourceKind"]) if entry["sourceKind"] else None),
            ownership_group=entry["ownershipGroup"],
            ownership_cluster=entry["ownershipCluster"],
            registered=entry["registered"],
            party_slug=entry.get("partySlug"))
        for entry in REGISTRY_DOC["publishers"])


REGISTRY = build_registry()


def build_candidate(doc: dict[str, Any]) -> CivicCandidateClaim:
    candidate = doc["candidate"]
    return CivicCandidateClaim(
        candidate_id=candidate["candidateId"],
        fingerprint=candidate["fingerprint"],
        record_kind=CivicRecordKind(candidate["recordKind"]),
        operation=CivicRecordOperation(candidate["operation"]),
        subject_id=candidate["subjectId"],
        record_key=candidate["recordKey"],
        event_date=date.fromisoformat(candidate["eventDate"]),
        category=CivicClaimCategory(candidate["category"]),
        content_class=CivicContentClass(candidate["contentClass"]),
        durable=candidate["durable"],
        claim_text=candidate["claimText"])


def build_evidence(entry: dict[str, Any]) -> CivicEvidenceItem:
    source_kind = CivicSourceKind(entry["sourceKind"]) if entry["sourceKind"] else None
    item = CivicEvidenceItem(
        evidence_id=entry["evidenceId"],
        publisher_id=entry["publisherId"],
        source_kind=source_kind,
        evidence_tier=CivicEvidenceTier(entry["civicEvidenceTier"]),
        stance=CivicEvidenceStance(entry["stance"]),
        canonical_url=entry["canonicalUrl"],
        title=entry["title"],
        subject_id=entry["subjectId"],
        event_date=date.fromisoformat(entry["eventDate"]),
        category=CivicClaimCategory(entry["category"]),
        published_at=datetime.fromisoformat(entry["publishedAt"]),
        accessible_full_text=entry["accessibleFullText"],
        paywalled=entry["paywalled"],
        anonymous_source_dependency=entry["anonymousSourceDependency"],
        discovery_only=entry["discoveryOnly"],
        party_slug=entry.get("partySlug"),
        url=entry["url"],
        excerpt=entry["excerpt"])
    # The corpus states the plan 4.1 tier; the engine derives it from the source kind. A case
    # that disagrees would silently exercise a different rule than the one it documents.
    if item.source_tier is not CivicSourceTier(entry["sourceTier"]):
        raise AssertionError(
            f"{entry['evidenceId']}: corpus tier {entry['sourceTier']} is not the tier the "
            f"engine derives ({item.source_tier.value})")
    return item


def build_dedup_context(doc: dict[str, Any]) -> CivicDedupContext | None:
    context = doc.get("dedupContext")
    if context is None:
        return None
    return CivicDedupContext.build(
        datetime.fromisoformat(context["now"]),
        sealed_candidates=[
            SealedCandidateRecord(
                candidate_id=entry["candidateId"],
                fingerprint=entry["fingerprint"],
                canonical_url=entry["canonicalUrl"],
                sealed_at=datetime.fromisoformat(entry["sealedAt"]))
            for entry in context["priorSealedCandidates"]],
        published_fingerprints=[
            PublishedFingerprintRecord(
                fingerprint=entry["fingerprint"],
                batch_id=entry["batchId"],
                published_at=datetime.fromisoformat(entry["publishedAt"]))
            for entry in context["publishedFingerprints"]])


@dataclass(frozen=True)
class CaseInputs:
    """One approved evidence case as production inputs."""

    case_id: str
    candidate: CivicCandidateClaim
    evidence: tuple[CivicEvidenceItem, ...]
    additional_evidence: tuple[CivicEvidenceItem, ...]
    dedup_context: CivicDedupContext | None
    expects_retry: bool


def build_case(doc: dict[str, Any]) -> CaseInputs:
    retry = doc.get("retry")
    return CaseInputs(
        case_id=doc["id"],
        candidate=build_candidate(doc),
        evidence=tuple(build_evidence(entry) for entry in doc["evidence"]),
        additional_evidence=tuple(
            build_evidence(entry) for entry in (retry or {}).get("additionalEvidence", [])),
        dedup_context=build_dedup_context(doc),
        expects_retry=retry is not None)


def evaluate_case(doc: dict[str, Any]) -> CivicResearchQualification:
    """Run the real engine over one approved case, including its bounded research attempt."""
    from beehive.civic.evidence import qualify_with_bounded_research

    case = build_case(doc)
    return qualify_with_bounded_research(
        case.candidate, case.evidence, registry=REGISTRY,
        additional_evidence=case.additional_evidence, dedup_context=case.dedup_context)


def build_queue(doc: dict[str, Any]) -> tuple[CivicRankedCandidate, ...]:
    return tuple(
        CivicRankedCandidate(
            candidate_id=entry["candidateId"],
            rank=entry["rank"],
            fingerprint=entry["fingerprint"],
            record_kind=CivicRecordKind(entry["recordKind"]))
        for entry in doc["queue"])


def run_mode(doc: dict[str, Any]) -> CivicRunMode:
    return CivicRunMode(doc["run"]["mode"])


def check_expectation(
    qualification: CivicQualification, expected: dict[str, Any], label: str,
) -> None:
    """Assert one engine decision against one approved expectation block, field for field.

    Reason codes are compared as an ordered list, so a missing code, an unrelated extra code and
    a reordering are each a failure."""
    eligibility = _ELIGIBILITY_BY_DISPOSITION[qualification.disposition]
    assert eligibility == expected["eligibility"], (
        f"{label}: engine says {eligibility}, corpus says {expected['eligibility']}")
    assert qualification.outcome is CivicQualificationOutcome(expected["outcome"]), (
        f"{label}: engine outcome {qualification.outcome.value} is not "
        f"{expected['outcome']}")
    assert list(qualification.evidence_used) == expected["evidenceUsed"], (
        f"{label}: cited evidence {list(qualification.evidence_used)} is not "
        f"{expected['evidenceUsed']}")
    assert list(qualification.evidence_excluded) == expected["evidenceExcluded"], (
        f"{label}: excluded evidence {list(qualification.evidence_excluded)} is not "
        f"{expected['evidenceExcluded']}")
    assert list(qualification.ownership_groups) == expected["ownershipGroups"], (
        f"{label}: ownership groups {list(qualification.ownership_groups)} are not "
        f"{expected['ownershipGroups']}")
    assert list(qualification.ownership_clusters) == expected["ownershipClusters"], (
        f"{label}: ownership clusters {list(qualification.ownership_clusters)} are not "
        f"{expected['ownershipClusters']}")

    emitted = [reason.value for reason in qualification.reasons]
    assert emitted == expected["reasonCodes"], (
        f"{label}: engine emitted {emitted}, corpus expects {expected['reasonCodes']} "
        f"(missing {[c for c in expected['reasonCodes'] if c not in emitted]}, "
        f"unexpected {[c for c in emitted if c not in expected['reasonCodes']]})")
    unknown = [code for code in emitted if code not in SCHEMA["reasonCodes"]]
    assert not unknown, f"{label}: engine emitted codes outside the closed vocabulary {unknown}"
    required = _DISPOSITION_CLASS[qualification.disposition]
    offending = [
        reason.value for reason in qualification.reasons
        if reason_disposition(reason) is not required]
    assert not offending, (
        f"{label}: a {qualification.disposition.value} decision cannot carry {offending}")
    assert emitted == sorted(emitted), f"{label}: reason codes {emitted} are not ordered"


def policy_signature(doc: dict[str, Any]) -> tuple:
    """Everything about a case the policy is allowed to read, and nothing that identifies it.

    Names, ids, dates and publisher identities are deliberately absent: what a rule may use is a
    record kind, a category's rule, a content class, durability, and the tier, accessibility and
    agreement of each evidence item, plus how many distinct owners and clusters the cited media
    resolves to. Two cases with the same signature must therefore decide the same way."""
    case = build_case(doc)
    candidate = case.candidate
    items = case.evidence + case.additional_evidence

    def item_signature(item: CivicEvidenceItem) -> tuple:
        publisher = REGISTRY.get(item.publisher_id)
        if item.party_slug is None:
            party = "not-a-party-source"
        else:
            party = "own-party" if item.party_slug == candidate.subject_id else "other-party"
        return (
            item.source_tier.value,
            item.evidence_tier.value,
            # Stance is deliberately absent: the evidence policy never reads it. Whether an item
            # supports, corroborates or merely contextualises a claim is the verification
            # stage's input, and no rule in `beehive.civic.evidence` branches on it.
            publisher.kind.value if publisher is not None else "unknown",
            publisher is not None and publisher.is_evidence_capable,
            item.accessible_full_text,
            item.paywalled,
            item.anonymous_source_dependency,
            item.discovery_only,
            item.subject_id == candidate.subject_id,
            item.event_date == candidate.event_date,
            item.category is candidate.category,
            party)

    groups = {
        REGISTRY.get(item.publisher_id).ownership_group for item in items
        if REGISTRY.get(item.publisher_id) is not None
        and REGISTRY.get(item.publisher_id).kind is CivicPublisherKind.MEDIA
        and REGISTRY.get(item.publisher_id).ownership_group}
    clusters = {
        REGISTRY.get(item.publisher_id).ownership_cluster for item in items
        if REGISTRY.get(item.publisher_id) is not None
        and REGISTRY.get(item.publisher_id).kind is CivicPublisherKind.MEDIA
        and REGISTRY.get(item.publisher_id).ownership_cluster}

    dedup = ()
    if case.dedup_context is not None:
        dedup = (
            tuple(sorted(
                (record.fingerprint == candidate.fingerprint,
                 record.canonical_url in {item.canonical_url for item in items},
                 case.dedup_context.now - record.sealed_at)
                for record in case.dedup_context.sealed_candidates)),
            tuple(sorted(
                (record.fingerprint == candidate.fingerprint,
                 case.dedup_context.now - record.published_at)
                for record in case.dedup_context.published_fingerprints)))

    return (
        candidate.record_kind.value,
        candidate.operation.value,
        candidate.category in HARD_EXCLUDED_CATEGORIES,
        _category_rule_signature(candidate.category),
        candidate.content_class is CivicContentClass.FACTUAL,
        candidate.durable,
        tuple(sorted(item_signature(item) for item in case.evidence)),
        tuple(sorted(item_signature(item) for item in case.additional_evidence)),
        len(groups),
        len(clusters),
        dedup)


def _category_rule_signature(category: CivicClaimCategory) -> tuple:
    rule = SCHEMA["categoryEvidenceRules"][category.value]
    return (
        rule["publishable"], tuple(rule["sufficientTiers"]), rule["mediaOnlyPublishable"],
        rule["requiresMatchingParty"])


__all__ = [
    "EVIDENCE_CASES",
    "RUN_CASES",
    "REGISTRY",
    "SCHEMA",
    "CaseInputs",
    "build_case",
    "build_candidate",
    "build_dedup_context",
    "build_evidence",
    "build_queue",
    "build_registry",
    "check_expectation",
    "evaluate_case",
    "policy_signature",
    "run_mode",
    "CivicPolicyReason",
]
