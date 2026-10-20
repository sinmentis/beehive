# tests/civic/test_evidence_policy_engine.py
"""The Civic evidence engine against every approved case in the fixture corpus.

Nothing here restates a policy expectation. Each test loads the approved cases through
`tests/civic/evidence_policy_adapter.py`, maps them onto the real `beehive.civic` types, runs the
real engine and compares against what the corpus says, so the corpus stays the single statement
of what the policy decides and this file only proves the engine agrees with it.

Production code never reads these files: the adapter lives entirely in the test tree, and
`beehive.civic.evidence` has no idea the corpus exists.
"""
from __future__ import annotations

import pytest

from beehive.civic.batch_policy import select_batch
from beehive.civic.evidence import (CivicPublicationDisposition, CivicQualificationOutcome,
                                    qualify_candidate)
from beehive.civic.reasons import CivicPolicyReason
from tests.civic.evidence_policy_adapter import (EVIDENCE_CASES, REGISTRY, RUN_CASES,
                                                 build_case, build_queue, check_expectation,
                                                 evaluate_case, policy_signature, run_mode)

EVIDENCE_PARAMS = [pytest.param(doc, id=doc["id"]) for doc in EVIDENCE_CASES]
RUN_PARAMS = [pytest.param(doc, id=doc["id"]) for doc in RUN_CASES]


def test_corpus_is_loaded() -> None:
    assert EVIDENCE_CASES, "no evidence cases were loaded"
    assert RUN_CASES, "no run cases were loaded"


@pytest.mark.parametrize("doc", EVIDENCE_PARAMS)
def test_engine_reproduces_the_approved_decision(doc: dict) -> None:
    result = evaluate_case(doc)
    check_expectation(result.after, doc["expected"], doc["id"])


@pytest.mark.parametrize("doc", EVIDENCE_PARAMS)
def test_engine_reproduces_the_pre_research_decision(doc: dict) -> None:
    """A retry case also pins the decision that justified spending the attempt."""
    retry = doc.get("retry")
    if retry is None:
        return
    result = evaluate_case(doc)
    check_expectation(result.before, retry["before"], f"{doc['id']}.retry.before")


@pytest.mark.parametrize("doc", EVIDENCE_PARAMS)
def test_research_attempts_stay_bounded(doc: dict) -> None:
    result = evaluate_case(doc)
    retry = doc.get("retry")
    assert result.max_attempts == 1
    if retry is None:
        assert result.attempts_used == 0
        assert CivicPolicyReason.RESEARCH_RETRY_RESOLVED not in result.after.reasons
        assert CivicPolicyReason.RESEARCH_RETRY_EXHAUSTED not in result.after.reasons
        assert result.after is result.before
        return
    assert result.attempts_used == retry["attemptsUsed"] == retry["maxAttempts"] == 1
    assert result.additional_evidence_ids == tuple(
        sorted(entry["evidenceId"] for entry in retry["additionalEvidence"]))
    marker = (CivicPolicyReason.RESEARCH_RETRY_RESOLVED if result.after.is_publishable
              else CivicPolicyReason.RESEARCH_RETRY_EXHAUSTED)
    assert marker in result.after.reasons
    assert not result.before.is_publishable, "a publishable candidate needs no research"


@pytest.mark.parametrize("doc", EVIDENCE_PARAMS)
def test_hard_decisions_never_spend_a_research_attempt(doc: dict) -> None:
    """An exclusion, a duplicate and an out-of-scope record kind are never retried."""
    case = build_case(doc)
    before = qualify_candidate(
        case.candidate, case.evidence, registry=REGISTRY, dedup_context=case.dedup_context)
    if before.outcome in (CivicQualificationOutcome.SCREENED_OUT,
                          CivicQualificationOutcome.DUPLICATE,
                          CivicQualificationOutcome.DEFERRED_UNSUPPORTED):
        assert not before.research_retry_eligible, f"{doc['id']} would spend a research attempt"
        assert evaluate_case(doc).attempts_used == 0


@pytest.mark.parametrize("doc", EVIDENCE_PARAMS)
def test_decision_is_independent_of_evidence_order(doc: dict) -> None:
    case = build_case(doc)
    forward = qualify_candidate(
        case.candidate, case.evidence, registry=REGISTRY, dedup_context=case.dedup_context)
    reverse = qualify_candidate(
        case.candidate, tuple(reversed(case.evidence)), registry=REGISTRY,
        dedup_context=case.dedup_context)
    assert forward.reasons == reverse.reasons
    assert forward.outcome is reverse.outcome
    assert forward.evidence_used == reverse.evidence_used
    assert forward.evidence_excluded == reverse.evidence_excluded
    assert forward.ownership_groups == reverse.ownership_groups
    assert forward.ownership_clusters == reverse.ownership_clusters


@pytest.mark.parametrize("doc", EVIDENCE_PARAMS)
def test_decision_is_idempotent(doc: dict) -> None:
    first, second = evaluate_case(doc).after, evaluate_case(doc).after
    assert first == second


@pytest.mark.parametrize("doc", EVIDENCE_PARAMS)
def test_discovery_material_is_never_cited(doc: dict) -> None:
    case = build_case(doc)
    result = evaluate_case(doc)
    discovery = {
        item.evidence_id for item in case.evidence + case.additional_evidence
        if item.discovery_only}
    assert discovery.isdisjoint(result.after.evidence_used), (
        f"{doc['id']} cited discovery tier material")


@pytest.mark.parametrize("doc", EVIDENCE_PARAMS)
def test_dedup_is_only_evaluated_with_a_context(doc: dict) -> None:
    case = build_case(doc)
    result = evaluate_case(doc)
    dedup_codes = [
        reason for reason in result.after.reasons
        if reason.value.startswith(("dedup_", "duplicate_"))]
    if case.dedup_context is None:
        assert result.after.dedup is None
        assert not dedup_codes
    elif result.after.disposition is not CivicPublicationDisposition.DEFERRED:
        assert result.after.dedup is not None
        assert dedup_codes == [result.after.dedup.reason]


@pytest.mark.parametrize("doc", RUN_PARAMS)
def test_batch_selection_reproduces_the_approved_run(doc: dict) -> None:
    expected = doc["expected"]
    selection = select_batch(build_queue(doc), mode=run_mode(doc))
    assert selection.published_cap == doc["run"]["publishedCap"]
    assert list(selection.published_candidate_ids) == expected["publishedCandidateIds"]
    assert list(selection.deferred_candidate_ids) == expected["deferredCandidateIds"]
    assert [reason.value for reason in selection.reasons] == expected["reasonCodes"]
    assert {
        candidate_id: reason.value
        for candidate_id, reason in selection.deferral_reason_by_candidate_id.items()
    } == expected["deferralReasonByCandidateId"]


@pytest.mark.parametrize("doc", RUN_PARAMS)
def test_batch_selection_is_independent_of_queue_order(doc: dict) -> None:
    queue = build_queue(doc)
    mode = run_mode(doc)
    assert select_batch(queue, mode=mode) == select_batch(tuple(reversed(queue)), mode=mode)


# --- Reason codes are compared exactly ----------------------------------------------------------
#
# `expected.reasonCodes` is the complete set the engine must emit, so an unrelated extra code is
# a failure. Two cases whose policy-relevant inputs are identical must therefore record identical
# expectations, and `policy_signature` is what makes that checkable: it carries everything a rule
# is allowed to read and nothing that identifies the case. These pairs were the corpus's two
# known inconsistencies, and they are pinned here so a future edit cannot reopen them.

_INPUT_IDENTICAL_PAIRS = (
    ("official-parliament-singleton-valid", "significance-durable-legislation-valid"),
    ("significance-opinion-excluded", "significance-poll-excluded"),
)


def _case(case_id: str) -> dict:
    for doc in EVIDENCE_CASES:
        if doc["id"] == case_id:
            return doc
    raise AssertionError(f"case {case_id} is missing from the corpus")


@pytest.mark.parametrize("doc", EVIDENCE_PARAMS)
def test_engine_emits_no_reason_beyond_the_approved_set(doc: dict) -> None:
    """No unrelated extra code, in the decision or in the pre-research decision."""
    result = evaluate_case(doc)
    blocks = [(result.after, doc["expected"], doc["id"])]
    if "retry" in doc:
        blocks.append((result.before, doc["retry"]["before"], f"{doc['id']}.retry.before"))
    for qualification, expected, label in blocks:
        approved = set(expected["reasonCodes"])
        extra = sorted(
            reason.value for reason in qualification.reasons if reason.value not in approved)
        assert not extra, f"{label}: engine emitted unapproved reason codes {extra}"


@pytest.mark.parametrize("left_id,right_id", _INPUT_IDENTICAL_PAIRS)
def test_input_identical_cases_record_the_same_code_set(left_id: str, right_id: str) -> None:
    left, right = _case(left_id), _case(right_id)
    assert policy_signature(left) == policy_signature(right), (
        f"{left_id} and {right_id} no longer have identical policy inputs")
    assert left["expected"]["reasonCodes"] == right["expected"]["reasonCodes"], (
        f"{left_id} and {right_id} have identical inputs but different expectations, so no "
        f"engine could satisfy both")


@pytest.mark.parametrize("left_id,right_id", _INPUT_IDENTICAL_PAIRS)
def test_input_identical_cases_decide_identically(left_id: str, right_id: str) -> None:
    """Identical inputs must produce an identical decision, including identical codes."""
    left, right = evaluate_case(_case(left_id)).after, evaluate_case(_case(right_id)).after
    assert left.disposition is right.disposition
    assert left.outcome is right.outcome
    assert left.reasons == right.reasons
