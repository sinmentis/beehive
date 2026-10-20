"""Running the pure Civic evidence engine against a real database, durably.

`beehive.civic.evidence` decides; nothing in it touches a clock, a socket or a table. This module
is the part that has to survive: it reads the inputs out of the Civic tables, hands them to the
engine, and records the decision and its consequences under one lock so a worker that dies
halfway through leaves the database in a state its replacement can finish rather than a state it
has to guess about.

The shape of one Run's work:

```text
CivicDocument[]            (connectors)
  -> cluster_civic_documents        one cluster per development, order independent
  -> ingest_civic_documents         Candidate + consumed-item claim, one transaction
  -> qualify_civic_candidate        engine decision + receipt + settlement, one transaction
  -> begin_civic_research           one bounded pass, spent durably before any work runs
  -> qualify_civic_candidate        the decision that stands, recorded next to the first
```

What makes it safe to run twice:

* **Every write is fenced on the Run's claim token.** A worker whose lease was recovered writes
  nothing and is told so by a `None` return, the same convention `db/civic_runs.py` and
  `db/civic_batches.py` already use for a claim that has moved on.
* **`now` is sampled after `BEGIN IMMEDIATE`, never before.** A worker that blocked for a minute
  waiting for the write lock must fence on the time it actually got it, or it will stamp a
  deadline it has already passed.
* **An item can only be consumed once.** `civic_consumed_items` takes its claim in the same
  transaction that creates the Candidate, so re-clustering yesterday's articles cannot produce a
  second Candidate for the same development.
* **A decision carries its own inputs.** Every qualification writes a receipt hashing the claim,
  the evidence, the registry and the dedup membership it saw. Replaying identical inputs returns
  the stored decision; replaying changed inputs raises `CivicQualificationDriftError` instead of
  quietly overturning a decision other rows already depend on.
* **The research pass is spent before it runs.** `begin_civic_research` commits the reservation,
  so a crash costs the attempt. `resume_civic_research` finds a reservation whose outcome was
  never written, which is the only way a resumed worker can tell "I already spent this" from "I
  still have one".
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable, Sequence

from beehive.civic.clustering import (MAX_CLUSTERS_PER_RUN, CivicClusteringResult,
                                      CivicDocumentCluster, cluster_civic_documents)
from beehive.civic.documents import CivicDocument
from beehive.civic.evidence import (MAX_RESEARCH_ATTEMPTS, CivicCandidateClaim,
                                    CivicClaimCategory, CivicEvidenceItem,
                                    CivicOwnershipRegistry, CivicPublicationDisposition,
                                    CivicQualification,
                                    CivicQualificationOutcome, CivicSourceKind,
                                    qualify_candidate, qualify_with_bounded_research)
from beehive.civic.reasons import CivicPolicyReason
from beehive.db.civic_batches import _adopt_locked
from beehive.db.civic_candidates import (create_candidate_locked, get_candidate,
                                         list_candidate_evidence)
from beehive.db.civic_consumed_items import claim_consumed_item, list_consumed_item_keys
from beehive.db.civic_dedup import build_dedup_context
from beehive.db.civic_qualifications import (CivicQualificationRecord, get_qualification,
                                             get_latest_qualification, record_qualification)
from beehive.db.civic_verifications import (pending_research_attempt,
                                            reserve_research_attempt_locked)
from beehive.domain.civic import (CivicCandidate, CivicCandidateStatus, CivicEvidence,
                                  is_terminal_candidate_status, require_candidate_transition)
from beehive.title_similarity import DEFAULT_SIMILARITY_THRESHOLD

# Bumped whenever a change to beehive.civic.evidence, beehive.civic.dedup or the inputs this
# module assembles could decide a Candidate differently. Stored on every receipt, so a decision
# is always attributable to a build rather than to "the engine, at some point".
CIVIC_ENGINE_VERSION = "civic-evidence/1"

# What each engine outcome does to the Candidate. Every outcome settles except the two that are
# still waiting for a stage that has not run yet: a qualified Candidate is waiting on the
# verifier, and an evidence deferral with budget left is waiting on its one research pass.
# Nothing is left in an unexplained state -- an unsettled Candidate blocks the seal by design,
# so "we decided nothing" would stall the Batch rather than quietly publish less.
#
# DEFERRED_CAPACITY is deliberately absent. Capacity is not an evidence finding and must never be
# recorded as one: the daily cap is applied by the Civic Batch seal, which moves the overflow to
# the non-terminal DEFERRED_CAPACITY status so a later Run can adopt it. Rejecting a Candidate
# here for capacity would make it terminal, and a terminal Candidate can never come back.
_REJECTION_CODE_BY_OUTCOME = {
    CivicQualificationOutcome.SCREENED_OUT: "screened_out",
    CivicQualificationOutcome.DEFERRED_UNSUPPORTED: "deferred_unsupported",
    CivicQualificationOutcome.DEFERRED_EVIDENCE: "deferred_evidence",
}


class CivicPipelineError(ValueError):
    """An input this module cannot act on: a Candidate that is not in the Run, evidence with no
    publisher, a claim whose fingerprint is not the Candidate's. Distinct from a lost claim,
    which is a race and returns None."""


@dataclass(frozen=True)
class CivicIngestResult:
    """What one ingest pass created, reused and held back."""

    created_candidates: tuple[CivicCandidate, ...]
    reused_candidates: tuple[CivicCandidate, ...]
    skipped_item_keys: tuple[str, ...]
    deferred_clusters: tuple[CivicDocumentCluster, ...]
    clustering: CivicClusteringResult
    adopted_candidate_ids: tuple[int, ...] = ()

    @property
    def candidates(self) -> tuple[CivicCandidate, ...]:
        return self.created_candidates + self.reused_candidates

    @property
    def is_truncated(self) -> bool:
        return bool(self.deferred_clusters)


@dataclass(frozen=True)
class CivicQualificationOutput:
    """A decision, the record that proves it, and the Candidate as it stands afterwards."""

    record: CivicQualificationRecord
    qualification: CivicQualification
    candidate: CivicCandidate
    replayed: bool = False

    @property
    def is_publishable(self) -> bool:
        return self.record.is_publishable


def _fence(
    conn: sqlite3.Connection, run_id: int, claim_token: str, now: datetime,
) -> sqlite3.Row | None:
    """The Run row, only if this claim still owns it and its deadline has not arrived.

    Read inside the caller's BEGIN IMMEDIATE with a `now` sampled after the lock: fencing on the
    time a worker *asked* for the lock rather than the time it got it is how a run that blocked
    past its own deadline ends up writing anyway."""
    row = conn.execute(
        "SELECT id, watch_id, deadline_at FROM civic_runs "
        "WHERE id = ? AND claim_token = ? AND status = 'processing'",
        (run_id, claim_token)).fetchone()
    if row is None:
        return None
    if row["deadline_at"] and now >= datetime.fromisoformat(row["deadline_at"]):
        return None
    return row


def _building_batch(conn: sqlite3.Connection, run_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, watch_id, status FROM civic_batches WHERE run_id = ?", (run_id,)).fetchone()


def ingest_civic_documents(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    claim_token: str,
    documents: Iterable[CivicDocument],
    now: datetime,
    now_fn: Callable[[], datetime] | None = None,
    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    max_clusters: int = MAX_CLUSTERS_PER_RUN,
    clustering: CivicClusteringResult | None = None,
) -> CivicIngestResult | None:
    """Cluster this Run's documents into Candidates, claiming each source item as it goes.

    One transaction for the whole pass, so a Candidate and the claims on the items that produced
    it are always both there or both absent. Returns None -- writing nothing -- when the Run's
    claim no longer matches or its deadline has arrived.

    A cluster whose development already has a Candidate on this Watch (same `dedupe_key`) reuses
    it instead of inserting a second one, and appears under `reused_candidates`. A cluster whose
    items are already owned by a *different* Candidate raises: that is a fault in the caller's
    consumed-item bookkeeping, not a race to absorb quietly.

    Carryovers are adopted first, in this same transaction, and that happens *before* the
    documents are even looked at -- so a Run whose connectors returned nothing still takes over
    what yesterday's cap held back. An empty news day is exactly when there is room for it, and
    making adoption a consequence of finding new material would strand the overflow on precisely
    the days that could publish it.

    A caller that performs bounded work between clustering and persistence may provide that
    prepared `clustering`. The partition is validated under the write lock and then used as the
    sole Candidate plan; changing a document's recovered publication date cannot trigger a second
    ranking pass that displaces the document whose detail work was just completed.
    """
    source_documents = tuple(documents)
    conn.execute("BEGIN IMMEDIATE")
    try:
        now = now_fn() if now_fn is not None else now
        run = _fence(conn, run_id, claim_token, now)
        if run is None:
            result = None
        else:
            batch = _building_batch(conn, run_id)
            if batch is None:
                raise CivicPipelineError(f"Civic Run {run_id} has no Civic Batch")
            if batch["status"] != "building":
                raise CivicPipelineError(
                    f"cannot ingest into Civic Batch {batch['id']}: status is "
                    f"{batch['status']!r}, not 'building'")
            watch_id = run["watch_id"]
            adopted = _adopt_locked(
                conn, watch_id=watch_id, run_id=run_id, batch_id=batch["id"], now=now,
                limit=None)
            consumed_item_keys = list_consumed_item_keys(conn, watch_id)
            if clustering is None:
                clustering = cluster_civic_documents(
                    source_documents,
                    consumed_item_keys=consumed_item_keys,
                    similarity_threshold=similarity_threshold,
                    max_clusters=max_clusters)
            else:
                if len(clustering.clusters) > max_clusters:
                    raise CivicPipelineError(
                        f"prepared clustering has {len(clustering.clusters)} clusters; "
                        f"maximum is {max_clusters}")
                source_keys = {document.dedupe_key for document in source_documents}
                planned_documents = tuple(
                    document
                    for cluster in (*clustering.clusters, *clustering.deferred_clusters)
                    for document in cluster.documents
                )
                planned_keys = {document.dedupe_key for document in planned_documents}
                partitioned_keys = planned_keys | set(clustering.skipped_item_keys)
                if len(planned_keys) != len(planned_documents):
                    raise CivicPipelineError(
                        "prepared clustering repeats a Civic document")
                if partitioned_keys != source_keys:
                    raise CivicPipelineError(
                        "prepared clustering does not partition the supplied Civic documents")
                newly_consumed = planned_keys & set(consumed_item_keys)
                if newly_consumed:
                    raise CivicPipelineError(
                        "prepared clustering contains Civic documents consumed while it was "
                        "being hydrated")
            created: list[CivicCandidate] = []
            reused: list[CivicCandidate] = []
            for cluster in clustering.clusters:
                candidate, was_created = create_candidate_locked(
                    conn, batch_id=batch["id"], watch_id=watch_id, run_id=run_id,
                    batch_status=batch["status"], dedupe_key=cluster.cluster_key,
                    title=cluster.title, statement=_statement_for(cluster), now=now)
                (created if was_created else reused).append(candidate)
                for document in cluster.documents:
                    claim_consumed_item(
                        conn, watch_id=watch_id, run_id=run_id, candidate_id=candidate.id,
                        item_key=document.dedupe_key, source_key=document.source_key,
                        external_key=document.external_key,
                        canonical_url=document.canonical_url, now=now)
            result = CivicIngestResult(
                created_candidates=tuple(created),
                reused_candidates=tuple(reused),
                skipped_item_keys=clustering.skipped_item_keys,
                deferred_clusters=clustering.deferred_clusters,
                clustering=clustering,
                adopted_candidate_ids=tuple(adopted))
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return result


def _statement_for(cluster: CivicDocumentCluster) -> str:
    """The Candidate's provisional statement: the lead source's own words, never invented here.

    Drafting is an AI stage that has not run yet; until it does, a Candidate carries the excerpt
    the source published so nothing downstream has to treat an empty statement as meaningful."""
    lead = cluster.documents[0]
    for document in cluster.documents:
        if document.excerpt.strip():
            lead = document
            break
    return lead.excerpt.strip() or cluster.title


def _require_evidence_policy_fields(evidence: CivicEvidence) -> None:
    if not evidence.publisher_id.strip():
        raise CivicPipelineError(
            f"Civic Evidence {evidence.external_key!r} has no publisher_id and cannot be "
            "qualified")
    if not evidence.subject_id.strip():
        raise CivicPipelineError(
            f"Civic Evidence {evidence.external_key!r} has no subject_id and cannot be "
            "qualified")
    if evidence.claim_category is None:
        raise CivicPipelineError(
            f"Civic Evidence {evidence.external_key!r} has no claim_category and cannot be "
            "qualified")


def evidence_item_for(evidence: CivicEvidence) -> CivicEvidenceItem:
    """Convert a dated evidence row without defaulting missing ownership or policy fields."""
    _require_evidence_policy_fields(evidence)
    if evidence.event_date is None:
        raise CivicPipelineError(
            f"Civic Evidence {evidence.external_key!r} has no event_date and cannot be "
            "qualified")
    return CivicEvidenceItem(
        evidence_id=evidence.external_key,
        publisher_id=evidence.publisher_id,
        source_kind=(
            CivicSourceKind(evidence.source_kind) if evidence.source_kind else None),
        evidence_tier=evidence.tier,
        stance=evidence.stance,
        canonical_url=evidence.canonical_url or evidence.url,
        title=evidence.title,
        subject_id=evidence.subject_id,
        event_date=evidence.event_date,
        category=CivicClaimCategory(evidence.claim_category),
        published_at=evidence.published_at or evidence.captured_at,
        accessible_full_text=evidence.accessible_full_text,
        paywalled=evidence.paywalled,
        anonymous_source_dependency=evidence.anonymous_source_dependency,
        discovery_only=evidence.discovery_only,
        party_slug=evidence.party_slug,
        url=evidence.url,
        excerpt=evidence.excerpt)


def _evidence_payload(item: CivicEvidenceItem) -> dict:
    return {
        "evidence_id": item.evidence_id,
        "publisher_id": item.publisher_id,
        "source_kind": item.source_kind.value if item.source_kind else None,
        "evidence_tier": item.evidence_tier.value,
        "stance": item.stance.value,
        "canonical_url": item.canonical_url,
        "subject_id": item.subject_id,
        "event_date": item.event_date.isoformat(),
        "category": item.category.value,
        "published_at": item.published_at.isoformat(),
        "accessible_full_text": item.accessible_full_text,
        "paywalled": item.paywalled,
        "anonymous_source_dependency": item.anonymous_source_dependency,
        "discovery_only": item.discovery_only,
        "party_slug": item.party_slug,
    }


def qualification_input_hash(
    claim: CivicCandidateClaim,
    evidence: Sequence[CivicEvidenceItem],
    *,
    registry: CivicOwnershipRegistry,
    dedup_fingerprints: Sequence[str],
    dedup_urls: Sequence[str],
    engine_version: str = CIVIC_ENGINE_VERSION,
) -> str:
    """SHA-256 over everything that could change the decision, and nothing that could not.

    Sorted throughout, so the order rows came back in never moves the hash. The dedup *membership*
    is hashed rather than the instant it was read: a worker replaying the same decision an hour
    later is replaying, and folding the wall clock in would make every resume look like drift
    while hiding the changes that really are drift."""
    payload = {
        "engine_version": engine_version,
        "claim": {
            "candidate_id": claim.candidate_id,
            "fingerprint": claim.fingerprint,
            "record_kind": claim.record_kind.value,
            "operation": claim.operation.value,
            "subject_id": claim.subject_id,
            "record_key": claim.record_key,
            "event_date": claim.event_date.isoformat(),
            "category": claim.category.value,
            "content_class": claim.content_class.value,
            "durable": claim.durable,
            "claim_text": claim.claim_text,
        },
        "evidence": sorted(
            (_evidence_payload(item) for item in evidence),
            key=lambda entry: entry["evidence_id"]),
        "registry": sorted(
            ({
                "publisher_id": publisher.publisher_id,
                "kind": publisher.kind.value,
                "source_kind": publisher.source_kind.value if publisher.source_kind else None,
                "ownership_group": publisher.ownership_group,
                "ownership_cluster": publisher.ownership_cluster,
                "registered": publisher.registered,
                "party_slug": publisher.party_slug,
            } for publisher in registry.publishers),
            key=lambda entry: entry["publisher_id"]),
        "dedup_fingerprints": sorted(set(dedup_fingerprints)),
        "dedup_urls": sorted(set(dedup_urls)),
    }
    return _digest(payload)


def qualification_output_hash(qualification: CivicQualification) -> str:
    """SHA-256 over the decision itself, so a receipt proves what was decided as well as why."""
    dedup = qualification.dedup
    payload = {
        "candidate_id": qualification.candidate_id,
        "outcome": qualification.outcome.value,
        "disposition": qualification.disposition.value,
        "reasons": [reason.value for reason in qualification.reasons],
        "evidence_used": list(qualification.evidence_used),
        "evidence_excluded": list(qualification.evidence_excluded),
        "ownership_groups": list(qualification.ownership_groups),
        "ownership_clusters": list(qualification.ownership_clusters),
        "dedup": None if dedup is None else {
            "reason": dedup.reason.value,
            "is_duplicate": dedup.is_duplicate,
            "matched_candidate_id": dedup.matched_candidate_id,
            "matched_batch_id": dedup.matched_batch_id,
            "matched_canonical_url": dedup.matched_canonical_url,
        },
    }
    return _digest(payload)


def _digest(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _require_claim_matches(claim: CivicCandidateClaim, candidate: CivicCandidate) -> None:
    if claim.candidate_id != str(candidate.id):
        raise CivicPipelineError(
            f"claim is for Candidate {claim.candidate_id!r}, not {candidate.id}")
    if claim.fingerprint != candidate.dedupe_key:
        raise CivicPipelineError(
            f"Civic Candidate {candidate.id}: claim fingerprint {claim.fingerprint!r} is not the "
            f"Candidate's dedupe_key {candidate.dedupe_key!r}")


def qualify_civic_candidate(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    claim_token: str,
    claim: CivicCandidateClaim,
    registry: CivicOwnershipRegistry,
    now: datetime,
    now_fn: Callable[[], datetime] | None = None,
    engine_version: str = CIVIC_ENGINE_VERSION,
    qualification_scope: Callable[
        [CivicCandidateClaim, CivicQualification], CivicQualification
    ] | None = None,
) -> CivicQualificationOutput | None:
    """Decide one Candidate through the pure engine, record the decision, and settle it.

    One transaction: the receipt and the Candidate's new status commit together, so a Candidate
    can never be found settled with no record of why, or recorded as qualified while still
    sitting in the state it was in before.

    Which pass this is comes from the database, not the caller. When a first decision is already
    on record and the bounded research attempt has been spent, this is the second pass: the
    evidence the first decision saw becomes the baseline and everything captured since is the
    research pass's contribution, which is what lets `qualify_with_bounded_research` mark the
    outcome `research_retry_resolved` or `research_retry_exhausted`.

    Returns None -- writing nothing -- when the Run's claim no longer matches. Replaying an
    identical decision returns the stored record with `replayed=True` and writes nothing;
    replaying changed inputs raises `CivicQualificationDriftError`."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        now = now_fn() if now_fn is not None else now
        run = _fence(conn, run_id, claim_token, now)
        if run is None:
            output = None
        else:
            candidate_id = _candidate_id_from(claim)
            candidate = get_candidate(conn, candidate_id)
            if candidate is None or candidate.run_id != run_id:
                raise CivicPipelineError(
                    f"no Civic Candidate with id={candidate_id} in Civic Run {run_id}")
            _require_claim_matches(claim, candidate)

            from beehive.db.civic_evidence_captures import require_current_source_capture

            require_current_source_capture(conn, candidate_id, publication_only=True)
            unrecorded = pending_research_attempt(conn, candidate_id)
            if unrecorded is not None:
                raise CivicPipelineError(
                    f"Civic Candidate {candidate_id}: research attempt {unrecorded} was "
                    "reserved and its outcome was never recorded; record it before qualifying "
                    "on what it found")

            prior = get_qualification(conn, candidate_id, 0)
            attempts_used = _attempts_used(conn, candidate_id, prior)
            stored = get_qualification(conn, candidate_id, attempts_used)

            items = []
            for evidence in list_candidate_evidence(conn, candidate_id):
                if evidence.event_date is None:
                    _require_evidence_policy_fields(evidence)
                    continue
                items.append(evidence_item_for(evidence))
            context = build_dedup_context(
                conn, candidate.watch_id, now, exclude_candidate_id=candidate_id)
            input_hash = qualification_input_hash(
                claim, items, registry=registry,
                dedup_fingerprints=[record.fingerprint
                                    for record in context.sealed_candidates]
                + [record.fingerprint for record in context.published_fingerprints],
                dedup_urls=[record.canonical_url for record in context.sealed_candidates],
                engine_version=engine_version)

            if attempts_used == 0:
                qualification = qualify_candidate(
                    claim, items, registry=registry, dedup_context=context)
            else:
                baseline_ids = frozenset(prior.evidence_used) | frozenset(
                    prior.evidence_excluded)
                base = [item for item in items if item.evidence_id in baseline_ids]
                extra = [item for item in items if item.evidence_id not in baseline_ids]
                qualification = qualify_with_bounded_research(
                    claim, base, registry=registry, additional_evidence=extra,
                    dedup_context=context).after
            if qualification_scope is not None:
                qualification = qualification_scope(claim, qualification)

            record = record_qualification(
                conn, candidate_id=candidate_id, run_id=run_id, qualification=qualification,
                engine_version=engine_version, input_hash=input_hash,
                output_hash=qualification_output_hash(qualification),
                research_attempts_used=attempts_used, now=now,
                record_kind=claim.record_kind)
            replayed = stored is not None
            if not replayed:
                _settle(conn, candidate, qualification, attempts_used, now)
            output = CivicQualificationOutput(
                record=record, qualification=qualification,
                candidate=get_candidate(conn, candidate_id), replayed=replayed)
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return output


_SUBJECT_RESOLUTION_REASONS = frozenset({
    CivicPolicyReason.SUBJECT_AMBIGUOUS_V1,
    CivicPolicyReason.SUBJECT_UNSUPPORTED_V1,
})


def settle_destination_subject_if_claimed(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    claim_token: str,
    candidate_id: int,
    reason: CivicPolicyReason,
    now: datetime,
    now_fn: Callable[[], datetime] | None = None,
    engine_version: str,
) -> CivicQualificationOutput | None:
    """Settle a Candidate whose party cannot be resolved without inventing an identity.

    This is a destination-scope decision, not an evidence retry. It records the same explicit
    ``deferred_unsupported`` qualification shape as the normal policy path, but it does not
    construct a fake subject merely to call an engine whose claim type requires one.
    """
    if reason not in _SUBJECT_RESOLUTION_REASONS:
        raise ValueError(f"{reason.value} is not a subject-resolution reason")
    conn.execute("BEGIN IMMEDIATE")
    try:
        now = now_fn() if now_fn is not None else now
        run = _fence(conn, run_id, claim_token, now)
        if run is None:
            output = None
        else:
            candidate = get_candidate(conn, candidate_id)
            if candidate is None or candidate.run_id != run_id:
                raise CivicPipelineError(
                    f"no Civic Candidate with id={candidate_id} in Civic Run {run_id}")
            evidence = list_candidate_evidence(conn, candidate_id)
            qualification = CivicQualification(
                candidate_id=str(candidate_id),
                disposition=CivicPublicationDisposition.DEFERRED_UNSUPPORTED,
                outcome=CivicQualificationOutcome.DEFERRED_UNSUPPORTED,
                reasons=(reason,),
                evidence_used=(),
                evidence_excluded=tuple(sorted(item.external_key for item in evidence)),
                ownership_groups=(),
                ownership_clusters=(),
                assessments=(),
            )
            input_hash = _digest({
                "engine_version": engine_version,
                "candidate_id": candidate_id,
                "fingerprint": candidate.dedupe_key,
                "subject_resolution": reason.value,
                "evidence": [
                    {
                        "external_key": item.external_key,
                        "source_ref": item.source_ref,
                        "canonical_url": item.canonical_url,
                        "publisher_id": item.publisher_id,
                        "title": item.title,
                        "excerpt": item.excerpt,
                        "source_kind": item.source_kind,
                        "party_slug": item.party_slug,
                    }
                    for item in sorted(evidence, key=lambda item: item.external_key)
                ],
            })
            record = record_qualification(
                conn,
                candidate_id=candidate_id,
                run_id=run_id,
                qualification=qualification,
                engine_version=engine_version,
                input_hash=input_hash,
                output_hash=qualification_output_hash(qualification),
                research_attempts_used=0,
                now=now,
                record_kind=None,
            )
            if candidate.status is not CivicCandidateStatus.REJECTED:
                _reject_locked(conn, candidate, reason.value, now)
            output = CivicQualificationOutput(
                record=record,
                qualification=qualification,
                candidate=get_candidate(conn, candidate_id),
                replayed=candidate.status is CivicCandidateStatus.REJECTED,
            )
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return output


def _candidate_id_from(claim: CivicCandidateClaim) -> int:
    if not claim.candidate_id.isdigit():
        raise CivicPipelineError(
            f"claim candidate_id {claim.candidate_id!r} is not a Civic Candidate row id")
    return int(claim.candidate_id)


def _attempts_used(
    conn: sqlite3.Connection, candidate_id: int, prior: CivicQualificationRecord | None,
) -> int:
    """Which pass this is: 0 until the bounded research attempt has actually been spent.

    Read from `research_attempt_count`, the same durable counter `begin_civic_research`
    increments before any research runs, so a worker that reserved an attempt and crashed comes
    back to the second pass rather than silently re-deciding the first."""
    if prior is None:
        return 0
    row = conn.execute(
        "SELECT research_attempt_count FROM civic_candidates WHERE id = ?",
        (candidate_id,)).fetchone()
    return 1 if row["research_attempt_count"] >= 1 else 0


def _settle(
    conn: sqlite3.Connection,
    candidate: CivicCandidate,
    qualification: CivicQualification,
    attempts_used: int,
    now: datetime,
) -> None:
    """Move the Candidate to the state its decision implies, inside the caller's transaction.

    Qualified waits for the verifier (VERIFYING) rather than settling: `mark_candidate_verified`
    is the only path to VERIFIED and it requires a supporting verdict, so publishing from here
    would bypass verification entirely. An evidence deferral with its research pass still unspent
    waits too (RESEARCHING). Everything else settles now, with the reason as its rejection code,
    because an unsettled Candidate blocks the Batch seal and a decision nobody is going to revisit
    would stall the Run forever."""
    outcome = qualification.outcome
    if outcome is CivicQualificationOutcome.QUALIFIED:
        _set_status_locked(conn, candidate, CivicCandidateStatus.VERIFYING, now)
        return
    if (outcome is CivicQualificationOutcome.DEFERRED_EVIDENCE
            and attempts_used < MAX_RESEARCH_ATTEMPTS
            and candidate.research_attempt_count < MAX_RESEARCH_ATTEMPTS
            and qualification.research_retry_eligible):
        _set_status_locked(conn, candidate, CivicCandidateStatus.RESEARCHING, now)
        return
    if outcome is CivicQualificationOutcome.DUPLICATE and qualification.dedup is not None:
        code = qualification.dedup.reason.value
    elif (outcome is CivicQualificationOutcome.DEFERRED_UNSUPPORTED
          and CivicPolicyReason.DESTINATION_SCOPE_V1 in qualification.reasons):
        code = CivicPolicyReason.DESTINATION_SCOPE_V1.value
    elif outcome in _REJECTION_CODE_BY_OUTCOME:
        code = _REJECTION_CODE_BY_OUTCOME[outcome]
    else:
        raise CivicPipelineError(
            f"Civic Candidate {candidate.id}: the evidence policy cannot settle a "
            f"{outcome.value} outcome; capacity is decided by the Civic Batch seal")
    _reject_locked(conn, candidate, code, now)


def _set_status_locked(
    conn: sqlite3.Connection, candidate: CivicCandidate, target: CivicCandidateStatus,
    now: datetime,
) -> None:
    if candidate.status is target:
        return
    require_candidate_transition(candidate.status, target)
    conn.execute(
        "UPDATE civic_candidates SET status = ? WHERE id = ?", (target.value, candidate.id))


def _reject_locked(
    conn: sqlite3.Connection, candidate: CivicCandidate, rejection_code: str, now: datetime,
) -> None:
    require_candidate_transition(candidate.status, CivicCandidateStatus.REJECTED)
    conn.execute(
        "UPDATE civic_candidates SET status = 'rejected', settled_at = ?, rejection_code = ? "
        "WHERE id = ?", (now.isoformat(), rejection_code, candidate.id))


def begin_civic_research(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    claim_token: str,
    candidate_id: int,
    now: datetime,
    now_fn: Callable[[], datetime] | None = None,
) -> int | None:
    """Spend the Candidate's one research attempt, durably, before any research runs.

    Returns the attempt number, or None when there is nothing to spend it on: the claim has moved
    on, the budget is gone, or the Candidate's recorded decision was not an evidence deferral. The
    last case matters -- a screened-out or duplicate Candidate is decided by something more
    evidence cannot change, and spending the single attempt on it would burn the budget a real
    evidence gap needs.

    The reservation commits here, alone. That is deliberate: a worker that dies mid-pass must lose
    the attempt rather than silently buy itself another one, and `resume_civic_research` is how
    its replacement finds out."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        now = now_fn() if now_fn is not None else now
        run = _fence(conn, run_id, claim_token, now)
        if run is None:
            reserved = None
        else:
            candidate = get_candidate(conn, candidate_id)
            if candidate is None or candidate.run_id != run_id:
                raise CivicPipelineError(
                    f"no Civic Candidate with id={candidate_id} in Civic Run {run_id}")
            latest = get_latest_qualification(conn, candidate_id)
            if latest is None:
                raise CivicPipelineError(
                    f"Civic Candidate {candidate_id} has no qualification on record and cannot "
                    "spend a research attempt")
            if (not latest.is_research_retryable
                    or is_terminal_candidate_status(candidate.status)):
                reserved = None
            else:
                reserved = reserve_research_attempt_locked(
                    conn, candidate_id, max_attempts=MAX_RESEARCH_ATTEMPTS)
                if reserved is not None:
                    _set_status_locked(
                        conn, candidate, CivicCandidateStatus.RESEARCHING, now)
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return reserved


def resume_civic_research(conn: sqlite3.Connection, candidate_id: int) -> int | None:
    """The attempt number a dead worker reserved and never recorded an outcome for.

    A resumed worker calls this first. `None` means every reserved attempt has an outcome on
    record, so the Candidate is not waiting for anything; a number means the pass was paid for and
    its result still has to be written, with `beehive.db.civic_verifications.
    record_research_attempt`, before the Candidate can be qualified again."""
    return pending_research_attempt(conn, candidate_id)


__all__ = [
    "CIVIC_ENGINE_VERSION",
    "CivicIngestResult",
    "CivicPipelineError",
    "CivicQualificationOutput",
    "begin_civic_research",
    "evidence_item_for",
    "ingest_civic_documents",
    "qualification_input_hash",
    "qualification_output_hash",
    "qualify_civic_candidate",
    "resume_civic_research",
    "settle_destination_subject_if_claimed",
]
