"""Pure Research Session domain types and state-transition rules."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class ResearchSessionStatus(str, Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class ResearchSourceOrigin(str, Enum):
    OWNER = "owner"
    PLAN = "plan"


class ResearchRunStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class ResearchRunPhase(str, Enum):
    PLANNING = "planning"
    COLLECTING = "collecting"
    ENRICHING = "enriching"
    CLUSTERING = "clustering"
    ASSESSING = "assessing"
    SYNTHESIZING = "synthesizing"


class EvidenceSnapshotStatus(str, Enum):
    BUILDING = "building"
    SEALED = "sealed"


class EvidenceQuality(str, Enum):
    PRIMARY = "primary"
    REPORTING = "reporting"
    ANALYSIS = "analysis"
    COMMUNITY = "community"
    AGGREGATOR = "aggregator"


class SufficiencyState(str, Enum):
    SUFFICIENT = "sufficient"
    PARTIAL = "partial"
    INSUFFICIENT = "insufficient"


class ClaimProvenance(str, Enum):
    EVIDENCE = "evidence"
    MODEL_KNOWLEDGE = "model_knowledge"


class SynthesisSection(str, Enum):
    """Which of a Research Synthesis's six evidence-only core sections (CONTEXT.md order) --
    or the structurally-separate, citation-free model-knowledge slot -- a Synthesis Claim
    belongs to. This is a real, validated domain field, not a convention encoded inside `text`:
    beehive.research.synthesis.build_document dispatches on this enum directly rather than
    parsing a "[section] " prefix out of the claim's own text."""
    BOTTOM_LINE = "bottom_line"
    KEY_FINDINGS = "key_findings"
    SOURCE_AGREEMENTS = "source_agreements"
    SOURCE_CONFLICTS = "source_conflicts"
    UNKNOWNS = "unknowns"
    EVIDENCE_COVERAGE = "evidence_coverage"
    MODEL_KNOWLEDGE = "model_knowledge"


# The six evidence-only sections: every claim placed in one of these must be EVIDENCE-provenance
# and carry at least one citation -- including UNKNOWNS/EVIDENCE_COVERAGE. A claim describing "no
# gaps found" or "no conflicts found" is still required to cite the representative Evidence Items
# that were reviewed to reach that conclusion (this is what beehive.research.synthesis's prompt
# already instructs the model to do). This module deliberately does NOT add a second, uncited
# "evidence-gap statement" path for those two sections: an uncited claim would need its own
# separate validated mechanism (e.g. a distinct "gap"/"gap_reason" claim shape with no citation
# requirement), and weakening citation rules for two of the six core sections is worse than
# requiring every core claim -- gap or otherwise -- to point at the evidence that grounds it.
_CORE_SYNTHESIS_SECTIONS = frozenset({
    SynthesisSection.BOTTOM_LINE,
    SynthesisSection.KEY_FINDINGS,
    SynthesisSection.SOURCE_AGREEMENTS,
    SynthesisSection.SOURCE_CONFLICTS,
    SynthesisSection.UNKNOWNS,
    SynthesisSection.EVIDENCE_COVERAGE,
})


class ConversationRole(str, Enum):
    OWNER = "owner"
    ASSISTANT = "assistant"


class ConversationMessageStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"


_SESSION_TRANSITIONS = {
    ResearchSessionStatus.ACTIVE: frozenset({ResearchSessionStatus.ARCHIVED}),
    ResearchSessionStatus.ARCHIVED: frozenset({ResearchSessionStatus.ACTIVE}),
}

_RUN_TRANSITIONS = {
    ResearchRunStatus.PENDING: frozenset({
        ResearchRunStatus.PROCESSING,
        ResearchRunStatus.CANCELLED,
    }),
    ResearchRunStatus.PROCESSING: frozenset({
        ResearchRunStatus.COMPLETED,
        ResearchRunStatus.CANCELLED,
        ResearchRunStatus.FAILED,
        ResearchRunStatus.PENDING,
    }),
    ResearchRunStatus.COMPLETED: frozenset(),
    ResearchRunStatus.CANCELLED: frozenset(),
    ResearchRunStatus.FAILED: frozenset(),
}

_SNAPSHOT_TRANSITIONS = {
    EvidenceSnapshotStatus.BUILDING: frozenset({EvidenceSnapshotStatus.SEALED}),
    EvidenceSnapshotStatus.SEALED: frozenset(),
}


def require_session_transition(
    current: ResearchSessionStatus,
    target: ResearchSessionStatus,
) -> None:
    if target not in _SESSION_TRANSITIONS[current]:
        raise ValueError(f"invalid Research Session transition: {current.value} -> {target.value}")


def require_run_transition(current: ResearchRunStatus, target: ResearchRunStatus) -> None:
    if target not in _RUN_TRANSITIONS[current]:
        raise ValueError(f"invalid Research Run transition: {current.value} -> {target.value}")


def require_snapshot_transition(
    current: EvidenceSnapshotStatus,
    target: EvidenceSnapshotStatus,
) -> None:
    if target not in _SNAPSHOT_TRANSITIONS[current]:
        raise ValueError(f"invalid Evidence Snapshot transition: {current.value} -> {target.value}")


@dataclass(frozen=True)
class ResearchSession:
    id: int | None
    question: str
    status: ResearchSessionStatus
    created_at: datetime
    last_activity_at: datetime
    archived_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.question.strip():
            raise ValueError("Research Question must not be empty")
        if self.status is ResearchSessionStatus.ARCHIVED and self.archived_at is None:
            raise ValueError("archived Research Session needs archived_at")
        if self.status is ResearchSessionStatus.ACTIVE and self.archived_at is not None:
            raise ValueError("active Research Session cannot have archived_at")


@dataclass(frozen=True)
class ResearchSource:
    id: int | None
    session_id: int
    connector_type: str
    config: dict[str, Any]
    origin: ResearchSourceOrigin

    def __post_init__(self) -> None:
        if self.session_id <= 0:
            raise ValueError("session_id must be positive")
        if not self.connector_type.strip():
            raise ValueError("connector_type must not be empty")


@dataclass(frozen=True)
class ResearchPlanRevision:
    id: int | None
    run_id: int
    version: int
    plan_json: str
    rationale: str
    is_validated: bool
    created_at: datetime

    def __post_init__(self) -> None:
        if self.run_id <= 0 or self.version <= 0:
            raise ValueError("run_id and version must be positive")
        if not self.plan_json:
            raise ValueError("plan_json must not be empty")


@dataclass(frozen=True)
class ResearchRun:
    id: int | None
    session_id: int
    status: ResearchRunStatus
    phase: ResearchRunPhase | None
    requested_at: datetime
    started_at: datetime | None = None
    deadline_at: datetime | None = None
    completed_at: datetime | None = None
    claim_token: str | None = None
    cancel_requested: bool = False
    deep_fetch_count: int = 0
    error_code: str | None = None
    error_detail: str | None = None

    def __post_init__(self) -> None:
        if self.session_id <= 0:
            raise ValueError("session_id must be positive")
        if self.deep_fetch_count < 0:
            raise ValueError("deep_fetch_count must not be negative")
        if self.status is ResearchRunStatus.PROCESSING:
            if self.phase is None or self.claim_token is None:
                raise ValueError("processing Research Run needs phase and claim_token")
            if self.started_at is None or self.deadline_at is None:
                raise ValueError("processing Research Run needs started_at and deadline_at")
        elif self.phase is not None:
            raise ValueError("only a processing Research Run can have a phase")
        if self.status in {
            ResearchRunStatus.COMPLETED,
            ResearchRunStatus.CANCELLED,
            ResearchRunStatus.FAILED,
        } and self.completed_at is None:
            raise ValueError("terminal Research Run needs completed_at")
        if self.error_code is not None and self.status is not ResearchRunStatus.FAILED:
            raise ValueError("only a failed Research Run can have an error_code")


@dataclass(frozen=True)
class EvidenceItem:
    id: int | None
    session_id: int
    research_source_id: int
    external_key: str
    title: str
    url: str
    citation_number: int
    quality: EvidenceQuality
    snippet: str = ""
    full_text: str | None = None
    raw_metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.session_id <= 0 or self.research_source_id <= 0:
            raise ValueError("session_id and research_source_id must be positive")
        if self.citation_number <= 0:
            raise ValueError("citation_number must be positive")
        if not self.external_key or not self.title or not self.url:
            raise ValueError("Evidence Item needs external_key, title, and url")


@dataclass(frozen=True)
class EvidenceSnapshot:
    id: int | None
    session_id: int
    run_id: int
    sequence_number: int
    status: EvidenceSnapshotStatus
    created_at: datetime
    sealed_at: datetime | None = None

    def __post_init__(self) -> None:
        if min(self.session_id, self.run_id, self.sequence_number) <= 0:
            raise ValueError("snapshot identifiers must be positive")
        if self.status is EvidenceSnapshotStatus.SEALED and self.sealed_at is None:
            raise ValueError("sealed Evidence Snapshot needs sealed_at")
        if self.status is EvidenceSnapshotStatus.BUILDING and self.sealed_at is not None:
            raise ValueError("building Evidence Snapshot cannot have sealed_at")


@dataclass(frozen=True)
class EvidenceStateRevision:
    id: int | None
    session_id: int
    version: int
    snapshot_id: int
    evidence_item_ids: tuple[int, ...]
    created_at: datetime

    def __post_init__(self) -> None:
        if min(self.session_id, self.version, self.snapshot_id) <= 0:
            raise ValueError("evidence revision identifiers must be positive")
        if len(set(self.evidence_item_ids)) != len(self.evidence_item_ids):
            raise ValueError("Evidence State Revision cannot contain duplicate items")


@dataclass(frozen=True)
class EvidenceCluster:
    id: int | None
    snapshot_id: int
    evidence_item_ids: tuple[int, ...]

    def __post_init__(self) -> None:
        if self.snapshot_id <= 0:
            raise ValueError("snapshot_id must be positive")
        if not self.evidence_item_ids:
            raise ValueError("Evidence Cluster must contain at least one item")
        if len(set(self.evidence_item_ids)) != len(self.evidence_item_ids):
            raise ValueError("Evidence Cluster cannot contain duplicate items")


@dataclass(frozen=True)
class EvidenceCitation:
    evidence_item_id: int
    citation_number: int

    def __post_init__(self) -> None:
        if self.evidence_item_id <= 0 or self.citation_number <= 0:
            raise ValueError("citation identifiers must be positive")


@dataclass(frozen=True)
class SynthesisClaim:
    text: str
    section: SynthesisSection
    provenance: ClaimProvenance
    citations: tuple[EvidenceCitation, ...] = ()

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("Synthesis Claim text must not be empty")
        if self.section is SynthesisSection.MODEL_KNOWLEDGE:
            if self.provenance is not ClaimProvenance.MODEL_KNOWLEDGE:
                raise ValueError(
                    "model_knowledge section Synthesis Claim needs MODEL_KNOWLEDGE provenance")
            if self.citations:
                raise ValueError("model-knowledge Synthesis Claim cannot have citations")
        elif self.section in _CORE_SYNTHESIS_SECTIONS:
            if self.provenance is not ClaimProvenance.EVIDENCE:
                raise ValueError(
                    f"{self.section.value} section Synthesis Claim needs EVIDENCE provenance")
            if not self.citations:
                raise ValueError("evidence-backed Synthesis Claim needs citations")
        else:
            raise ValueError(f"unknown Synthesis Section: {self.section!r}")


@dataclass(frozen=True)
class ResearchSynthesis:
    id: int | None
    session_id: int
    version: int
    evidence_state_revision_id: int
    sufficiency_state: SufficiencyState
    claims: tuple[SynthesisClaim, ...]
    created_at: datetime
    model: str
    language_code: str

    def __post_init__(self) -> None:
        if min(self.session_id, self.version, self.evidence_state_revision_id) <= 0:
            raise ValueError("synthesis identifiers must be positive")
        if not self.claims:
            raise ValueError("Research Synthesis must contain at least one claim")
        if not self.model or not self.language_code:
            raise ValueError("Research Synthesis needs model and language_code")


@dataclass(frozen=True)
class ConversationMessage:
    id: int | None
    session_id: int
    sequence_number: int
    role: ConversationRole
    status: ConversationMessageStatus
    content: str
    created_at: datetime

    def __post_init__(self) -> None:
        if min(self.session_id, self.sequence_number) <= 0:
            raise ValueError("message identifiers must be positive")
        if not self.content.strip():
            raise ValueError("Conversation Message content must not be empty")
        if self.role is ConversationRole.OWNER and self.status is not ConversationMessageStatus.READY:
            raise ValueError("Owner messages must be ready")


@dataclass(frozen=True)
class ConversationMemory:
    session_id: int
    version: int
    content: str
    covers_through_message_id: int | None
    updated_at: datetime

    def __post_init__(self) -> None:
        if self.session_id <= 0 or self.version <= 0:
            raise ValueError("memory identifiers must be positive")
        if self.covers_through_message_id is not None and self.covers_through_message_id <= 0:
            raise ValueError("covers_through_message_id must be positive")


@dataclass(frozen=True)
class SufficiencyAssessment:
    state: SufficiencyState
    covered_sub_questions: tuple[str, ...]
    gaps: tuple[str, ...]
    contradictions: tuple[str, ...]
    new_evidence_changed_conclusions: bool

    @property
    def is_sufficient(self) -> bool:
        return self.state is SufficiencyState.SUFFICIENT
