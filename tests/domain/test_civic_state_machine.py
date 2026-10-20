# tests/domain/test_civic_state_machine.py
"""Exhaustive legal/illegal transition coverage for every Civic state machine.

Each machine is checked in both directions: every documented legal move is accepted, and every
move NOT in the table is refused. That second half is generated from the enum itself, so adding
a new status without deciding where it may go fails here rather than silently becoming
unreachable -- or, worse, silently reachable from everywhere.
"""
import itertools

import pytest

from beehive.domain.civic import (CivicBatchStatus, CivicCandidateStatus,
                                   CivicNotificationStatus, CivicRunPhase, CivicRunStatus,
                                   CivicVocabularyStatus, CivicWatchStatus,
                                   is_terminal_batch_status, is_terminal_candidate_status,
                                   is_terminal_notification_status, is_terminal_run_status,
                                   require_batch_transition, require_candidate_transition,
                                   require_notification_transition, require_run_phase_advance,
                                   require_run_transition, require_vocabulary_transition,
                                   require_watch_transition)

_LEGAL_WATCH = {
    (CivicWatchStatus.ACTIVE, CivicWatchStatus.PAUSED),
    (CivicWatchStatus.ACTIVE, CivicWatchStatus.RETIRED),
    (CivicWatchStatus.PAUSED, CivicWatchStatus.ACTIVE),
    (CivicWatchStatus.PAUSED, CivicWatchStatus.RETIRED),
}

_LEGAL_VOCABULARY = {
    (CivicVocabularyStatus.DRAFT, CivicVocabularyStatus.PUBLISHED),
    (CivicVocabularyStatus.PUBLISHED, CivicVocabularyStatus.SUPERSEDED),
}

_LEGAL_RUN = {
    (CivicRunStatus.PENDING, CivicRunStatus.PROCESSING),
    (CivicRunStatus.PENDING, CivicRunStatus.CANCELLED),
    (CivicRunStatus.PENDING, CivicRunStatus.FAILED),
    (CivicRunStatus.PROCESSING, CivicRunStatus.COMPLETED),
    (CivicRunStatus.PROCESSING, CivicRunStatus.CANCELLED),
    (CivicRunStatus.PROCESSING, CivicRunStatus.FAILED),
    (CivicRunStatus.PROCESSING, CivicRunStatus.PENDING),
}

_LEGAL_BATCH = {
    (CivicBatchStatus.BUILDING, CivicBatchStatus.SEALED),
    (CivicBatchStatus.BUILDING, CivicBatchStatus.FAILED),
    (CivicBatchStatus.SEALED, CivicBatchStatus.DELIVERING),
    (CivicBatchStatus.SEALED, CivicBatchStatus.QUARANTINED),
    (CivicBatchStatus.SEALED, CivicBatchStatus.FAILED),
    (CivicBatchStatus.DELIVERING, CivicBatchStatus.DELIVERED),
    (CivicBatchStatus.DELIVERING, CivicBatchStatus.SEALED),
    (CivicBatchStatus.DELIVERING, CivicBatchStatus.QUARANTINED),
    (CivicBatchStatus.DELIVERING, CivicBatchStatus.FAILED),
}

_LEGAL_CANDIDATE = {
    (CivicCandidateStatus.DRAFTED, CivicCandidateStatus.RESEARCHING),
    (CivicCandidateStatus.DRAFTED, CivicCandidateStatus.VERIFYING),
    (CivicCandidateStatus.DRAFTED, CivicCandidateStatus.REJECTED),
    (CivicCandidateStatus.RESEARCHING, CivicCandidateStatus.VERIFYING),
    (CivicCandidateStatus.RESEARCHING, CivicCandidateStatus.REJECTED),
    (CivicCandidateStatus.VERIFYING, CivicCandidateStatus.VERIFIED),
    (CivicCandidateStatus.VERIFYING, CivicCandidateStatus.REJECTED),
    (CivicCandidateStatus.VERIFYING, CivicCandidateStatus.RESEARCHING),
    (CivicCandidateStatus.VERIFIED, CivicCandidateStatus.SUPERSEDED),
    (CivicCandidateStatus.VERIFIED, CivicCandidateStatus.DEFERRED_CAPACITY),
    (CivicCandidateStatus.DEFERRED_CAPACITY, CivicCandidateStatus.VERIFIED),
    (CivicCandidateStatus.DEFERRED_CAPACITY, CivicCandidateStatus.REJECTED),
    # The AI carryover pair. A Candidate whose generation ran out of the Run's fixed AI budget,
    # or lost its bounded transient retry, is held rather than judged -- and it resumes at
    # VERIFYING, not VERIFIED, because it still has stages to finish and a verifier to face.
    (CivicCandidateStatus.VERIFYING, CivicCandidateStatus.DEFERRED_AI),
    (CivicCandidateStatus.DEFERRED_AI, CivicCandidateStatus.VERIFYING),
    (CivicCandidateStatus.DEFERRED_AI, CivicCandidateStatus.REJECTED),
}

_LEGAL_NOTIFICATION = {
    (CivicNotificationStatus.PENDING, CivicNotificationStatus.SENDING),
    (CivicNotificationStatus.PENDING, CivicNotificationStatus.SUPPRESSED),
    (CivicNotificationStatus.PENDING, CivicNotificationStatus.FAILED),
    (CivicNotificationStatus.SENDING, CivicNotificationStatus.SENT),
    (CivicNotificationStatus.SENDING, CivicNotificationStatus.PENDING),
    (CivicNotificationStatus.SENDING, CivicNotificationStatus.FAILED),
    (CivicNotificationStatus.SENDING, CivicNotificationStatus.SUPPRESSED),
}

_MACHINES = [
    ("watch", CivicWatchStatus, _LEGAL_WATCH, require_watch_transition),
    ("vocabulary", CivicVocabularyStatus, _LEGAL_VOCABULARY, require_vocabulary_transition),
    ("run", CivicRunStatus, _LEGAL_RUN, require_run_transition),
    ("batch", CivicBatchStatus, _LEGAL_BATCH, require_batch_transition),
    ("candidate", CivicCandidateStatus, _LEGAL_CANDIDATE, require_candidate_transition),
    ("notification", CivicNotificationStatus, _LEGAL_NOTIFICATION,
     require_notification_transition),
]


def _cases(legal: bool):
    for name, enum, table, checker in _MACHINES:
        for current, target in itertools.product(enum, enum):
            if ((current, target) in table) is legal:
                yield pytest.param(
                    checker, current, target, id=f"{name}:{current.value}->{target.value}")


@pytest.mark.parametrize("checker,current,target", list(_cases(legal=True)))
def test_legal_transition_is_accepted(checker, current, target):
    checker(current, target)


@pytest.mark.parametrize("checker,current,target", list(_cases(legal=False)))
def test_illegal_transition_is_refused(checker, current, target):
    with pytest.raises(ValueError, match="invalid Civic"):
        checker(current, target)


# ============================================================================
# Specific rules worth stating on their own, because they are product decisions
# ============================================================================


def test_a_retired_watch_can_never_be_revived():
    """Retiring is how an Owner permanently ends a commitment. Reviving would silently resume
    publication under a subject they already withdrew -- a new Watch is created instead."""
    for target in CivicWatchStatus:
        with pytest.raises(ValueError):
            require_watch_transition(CivicWatchStatus.RETIRED, target)


def test_a_published_vocabulary_can_never_return_to_draft():
    with pytest.raises(ValueError):
        require_vocabulary_transition(
            CivicVocabularyStatus.PUBLISHED, CivicVocabularyStatus.DRAFT)


def test_processing_run_may_return_to_pending_for_recovery():
    require_run_transition(CivicRunStatus.PROCESSING, CivicRunStatus.PENDING)


def test_terminal_run_statuses_are_dead_ends():
    for status in (CivicRunStatus.COMPLETED, CivicRunStatus.CANCELLED, CivicRunStatus.FAILED):
        assert is_terminal_run_status(status)
        for target in CivicRunStatus:
            with pytest.raises(ValueError):
                require_run_transition(status, target)


def test_terminal_batch_statuses_are_dead_ends_including_quarantine():
    for status in (CivicBatchStatus.DELIVERED, CivicBatchStatus.QUARANTINED,
                   CivicBatchStatus.FAILED):
        assert is_terminal_batch_status(status)
        for target in CivicBatchStatus:
            with pytest.raises(ValueError):
                require_batch_transition(status, target)


def test_a_delivered_batch_can_never_be_delivered_again():
    with pytest.raises(ValueError):
        require_batch_transition(CivicBatchStatus.DELIVERED, CivicBatchStatus.DELIVERING)


def test_a_building_batch_can_never_be_delivered_without_sealing_first():
    with pytest.raises(ValueError):
        require_batch_transition(CivicBatchStatus.BUILDING, CivicBatchStatus.DELIVERING)


def test_a_drafted_candidate_can_never_jump_straight_to_verified():
    with pytest.raises(ValueError):
        require_candidate_transition(
            CivicCandidateStatus.DRAFTED, CivicCandidateStatus.VERIFIED)


def test_a_verified_candidate_cannot_use_the_generic_rejection_transition():
    with pytest.raises(ValueError):
        require_candidate_transition(
            CivicCandidateStatus.VERIFIED, CivicCandidateStatus.REJECTED)


def test_a_rejected_candidate_can_never_be_published_later():
    assert is_terminal_candidate_status(CivicCandidateStatus.REJECTED)
    for target in CivicCandidateStatus:
        with pytest.raises(ValueError):
            require_candidate_transition(CivicCandidateStatus.REJECTED, target)


def test_a_verified_candidate_may_only_be_superseded_or_carried():
    """Two ways out, and they mean opposite things. SUPERSEDED is a correction replacing a
    statement that was published; DEFERRED_CAPACITY is the daily cap holding one back that was
    never published at all, and it is reversible precisely because nothing went out."""
    require_candidate_transition(CivicCandidateStatus.VERIFIED, CivicCandidateStatus.SUPERSEDED)
    require_candidate_transition(
        CivicCandidateStatus.VERIFIED, CivicCandidateStatus.DEFERRED_CAPACITY)
    allowed = {CivicCandidateStatus.SUPERSEDED, CivicCandidateStatus.DEFERRED_CAPACITY}
    for target in set(CivicCandidateStatus) - allowed:
        with pytest.raises(ValueError):
            require_candidate_transition(CivicCandidateStatus.VERIFIED, target)


def test_a_sent_notification_is_never_resent():
    assert is_terminal_notification_status(CivicNotificationStatus.SENT)
    for target in CivicNotificationStatus:
        with pytest.raises(ValueError):
            require_notification_transition(CivicNotificationStatus.SENT, target)


# ============================================================================
# Run phases only move forward
# ============================================================================

_PHASE_ORDER = [CivicRunPhase.GATHERING, CivicRunPhase.SCREENING, CivicRunPhase.RESEARCHING,
                CivicRunPhase.VERIFYING, CivicRunPhase.PUBLISHING]


@pytest.mark.parametrize(
    "current,target",
    [(a, b) for i, a in enumerate(_PHASE_ORDER) for b in _PHASE_ORDER[i + 1:]],
    ids=lambda phase: phase.value if hasattr(phase, "value") else str(phase))
def test_forward_phase_advance_is_accepted(current, target):
    require_run_phase_advance(current, target)


@pytest.mark.parametrize(
    "current,target",
    [(a, b) for i, a in enumerate(_PHASE_ORDER) for b in _PHASE_ORDER[:i + 1]],
    ids=lambda phase: phase.value if hasattr(phase, "value") else str(phase))
def test_backwards_or_repeated_phase_advance_is_refused(current, target):
    with pytest.raises(ValueError, match="invalid Civic Run phase advance"):
        require_run_phase_advance(current, target)


def test_phase_advance_may_skip_phases():
    """A run with nothing to research goes straight from screening to verifying -- skipping is
    fine, going backwards is not."""
    require_run_phase_advance(CivicRunPhase.SCREENING, CivicRunPhase.PUBLISHING)
