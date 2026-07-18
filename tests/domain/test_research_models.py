from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from beehive.domain.research import (
    ClaimProvenance,
    ConversationMessage,
    ConversationMessageStatus,
    ConversationRole,
    EvidenceCitation,
    EvidenceQuality,
    EvidenceSnapshot,
    EvidenceSnapshotStatus,
    ResearchRun,
    ResearchRunPhase,
    ResearchRunStatus,
    ResearchSession,
    ResearchSessionStatus,
    SynthesisClaim,
    SynthesisSection,
)


NOW = datetime(2026, 7, 17, tzinfo=timezone.utc)


def test_research_session_question_is_required_and_model_is_frozen():
    session = ResearchSession(
        id=1,
        question="What changed?",
        status=ResearchSessionStatus.ACTIVE,
        created_at=NOW,
        last_activity_at=NOW,
    )

    with pytest.raises(FrozenInstanceError):
        session.question = "Changed"  # type: ignore[misc]
    with pytest.raises(ValueError, match="must not be empty"):
        ResearchSession(
            id=None,
            question=" ",
            status=ResearchSessionStatus.ACTIVE,
            created_at=NOW,
            last_activity_at=NOW,
        )


def test_archived_session_requires_archived_at():
    with pytest.raises(ValueError, match="needs archived_at"):
        ResearchSession(
            id=1,
            question="Question",
            status=ResearchSessionStatus.ARCHIVED,
            created_at=NOW,
            last_activity_at=NOW,
        )


def test_processing_run_requires_claim_phase_and_deadline():
    with pytest.raises(ValueError, match="phase and claim_token"):
        ResearchRun(
            id=1,
            session_id=1,
            status=ResearchRunStatus.PROCESSING,
            phase=None,
            requested_at=NOW,
            started_at=NOW,
            deadline_at=NOW,
            claim_token="claim",
        )

    run = ResearchRun(
        id=1,
        session_id=1,
        status=ResearchRunStatus.PROCESSING,
        phase=ResearchRunPhase.COLLECTING,
        requested_at=NOW,
        started_at=NOW,
        deadline_at=NOW,
        claim_token="claim",
    )
    assert run.phase is ResearchRunPhase.COLLECTING


def test_snapshot_sealed_at_matches_status():
    with pytest.raises(ValueError, match="needs sealed_at"):
        EvidenceSnapshot(
            id=1,
            session_id=1,
            run_id=1,
            sequence_number=1,
            status=EvidenceSnapshotStatus.SEALED,
            created_at=NOW,
        )


def test_claim_provenance_enforces_citation_boundary():
    citation = EvidenceCitation(evidence_item_id=1, citation_number=1)
    claim = SynthesisClaim(
        text="Supported claim.",
        section=SynthesisSection.BOTTOM_LINE,
        provenance=ClaimProvenance.EVIDENCE,
        citations=(citation,),
    )
    assert claim.citations == (citation,)

    with pytest.raises(ValueError, match="needs citations"):
        SynthesisClaim(
            text="Unsupported.",
            section=SynthesisSection.BOTTOM_LINE,
            provenance=ClaimProvenance.EVIDENCE,
        )
    with pytest.raises(ValueError, match="cannot have citations"):
        SynthesisClaim(
            text="Background.",
            section=SynthesisSection.MODEL_KNOWLEDGE,
            provenance=ClaimProvenance.MODEL_KNOWLEDGE,
            citations=(citation,),
        )


def test_synthesis_section_vocabulary_is_controlled():
    assert {section.value for section in SynthesisSection} == {
        "bottom_line",
        "key_findings",
        "source_agreements",
        "source_conflicts",
        "unknowns",
        "evidence_coverage",
        "model_knowledge",
    }


def test_core_section_claim_requires_evidence_provenance():
    citation = EvidenceCitation(evidence_item_id=1, citation_number=1)
    with pytest.raises(ValueError, match="needs EVIDENCE provenance"):
        SynthesisClaim(
            text="Mislabeled core claim.",
            section=SynthesisSection.KEY_FINDINGS,
            provenance=ClaimProvenance.MODEL_KNOWLEDGE,
        )
    # unknowns/evidence_coverage are core sections too: still EVIDENCE, still cited -- no
    # separate uncited "evidence-gap statement" path exists (see the module docstring).
    for section in (SynthesisSection.UNKNOWNS, SynthesisSection.EVIDENCE_COVERAGE):
        with pytest.raises(ValueError, match="needs citations"):
            SynthesisClaim(text="No gaps found.", section=section, provenance=ClaimProvenance.EVIDENCE)
        gap_claim = SynthesisClaim(
            text="No gaps found, citing representative coverage.",
            section=section,
            provenance=ClaimProvenance.EVIDENCE,
            citations=(citation,),
        )
        assert gap_claim.citations == (citation,)


def test_model_knowledge_section_requires_model_knowledge_provenance():
    citation = EvidenceCitation(evidence_item_id=1, citation_number=1)
    with pytest.raises(ValueError, match="needs MODEL_KNOWLEDGE provenance"):
        SynthesisClaim(
            text="Mislabeled background note.",
            section=SynthesisSection.MODEL_KNOWLEDGE,
            provenance=ClaimProvenance.EVIDENCE,
            citations=(citation,),
        )


def test_owner_message_must_be_ready():
    with pytest.raises(ValueError, match="must be ready"):
        ConversationMessage(
            id=1,
            session_id=1,
            sequence_number=1,
            role=ConversationRole.OWNER,
            status=ConversationMessageStatus.PENDING,
            content="Question",
            created_at=NOW,
        )


def test_quality_vocabulary_is_controlled():
    assert {quality.value for quality in EvidenceQuality} == {
        "primary",
        "reporting",
        "analysis",
        "community",
        "aggregator",
    }
