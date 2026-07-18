import pytest

from beehive.domain.research import (
    EvidenceSnapshotStatus,
    ResearchRunStatus,
    ResearchSessionStatus,
    require_run_transition,
    require_session_transition,
    require_snapshot_transition,
)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (ResearchRunStatus.PENDING, ResearchRunStatus.PROCESSING),
        (ResearchRunStatus.PENDING, ResearchRunStatus.CANCELLED),
        (ResearchRunStatus.PROCESSING, ResearchRunStatus.PENDING),
        (ResearchRunStatus.PROCESSING, ResearchRunStatus.COMPLETED),
        (ResearchRunStatus.PROCESSING, ResearchRunStatus.CANCELLED),
        (ResearchRunStatus.PROCESSING, ResearchRunStatus.FAILED),
    ],
)
def test_allowed_run_transitions(current, target):
    require_run_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (ResearchRunStatus.PENDING, ResearchRunStatus.COMPLETED),
        (ResearchRunStatus.COMPLETED, ResearchRunStatus.PENDING),
        (ResearchRunStatus.CANCELLED, ResearchRunStatus.PROCESSING),
        (ResearchRunStatus.FAILED, ResearchRunStatus.PROCESSING),
    ],
)
def test_invalid_run_transitions_raise(current, target):
    with pytest.raises(ValueError, match="invalid Research Run transition"):
        require_run_transition(current, target)


def test_session_can_archive_and_unarchive_only():
    require_session_transition(ResearchSessionStatus.ACTIVE, ResearchSessionStatus.ARCHIVED)
    require_session_transition(ResearchSessionStatus.ARCHIVED, ResearchSessionStatus.ACTIVE)
    with pytest.raises(ValueError, match="invalid Research Session transition"):
        require_session_transition(ResearchSessionStatus.ACTIVE, ResearchSessionStatus.ACTIVE)


def test_snapshot_can_only_seal_once():
    require_snapshot_transition(EvidenceSnapshotStatus.BUILDING, EvidenceSnapshotStatus.SEALED)
    with pytest.raises(ValueError, match="invalid Evidence Snapshot transition"):
        require_snapshot_transition(EvidenceSnapshotStatus.SEALED, EvidenceSnapshotStatus.BUILDING)
