from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime

from beehive.db.evidence_state import get_latest_evidence_state_revision
from beehive.db.research_runs import (
    advance_research_run_phase,
    complete_research_run_if_claimed,
)
from beehive.db.research_syntheses import list_syntheses
from beehive.domain.research import ResearchRunPhase, ResearchRunStatus, SufficiencyState
from beehive.localization import Localizer
from beehive.research.orchestrator import (
    RunOutcomeStatus,
    SealedEvidenceOutcome,
)
from beehive.research.synthesis import generate_synthesis


async def run_synthesis_retry(
    conn: sqlite3.Connection,
    run_id: int,
    claim_token: str,
    session_id: int,
    question: str,
    localizer: Localizer,
    *,
    model: str,
    now_fn: Callable[[], datetime],
    client: object | None = None,
) -> SealedEvidenceOutcome:
    if not advance_research_run_phase(
        conn,
        run_id,
        claim_token,
        ResearchRunPhase.SYNTHESIZING,
    ):
        raise RuntimeError("Research Run claim was lost before synthesis retry")
    revision = get_latest_evidence_state_revision(conn, session_id)
    if revision is None or not revision.evidence_item_ids:
        raise ValueError("Research Session has no active evidence to synthesize")
    prior_syntheses = list_syntheses(conn, session_id)
    sufficiency = (
        prior_syntheses[-1].sufficiency_state
        if prior_syntheses
        else SufficiencyState.PARTIAL
    )
    now = now_fn()
    synthesis = await generate_synthesis(
        conn,
        session_id,
        run_id,
        claim_token,
        question,
        revision.id,
        sufficiency,
        localizer,
        now,
        model=model,
        now_fn=now_fn,
        client=client,
    )
    terminal = complete_research_run_if_claimed(
        conn,
        run_id,
        claim_token,
        ResearchRunStatus.COMPLETED,
        now_fn(),
        now_fn=now_fn,
    )
    if not terminal.ok:
        raise RuntimeError("Research Run claim was lost after synthesis retry")
    status = (
        RunOutcomeStatus.CANCELLED_WITH_EVIDENCE
        if terminal.committed_status is ResearchRunStatus.CANCELLED
        else RunOutcomeStatus.RESUMED_SEALED_SNAPSHOT
    )
    return SealedEvidenceOutcome(
        status=status,
        run_id=run_id,
        snapshot_id=revision.snapshot_id,
        evidence_state_revision_id=revision.id,
        synthesis_id=synthesis.id,
        sufficiency=None,
        rounds_completed=0,
        source_failures=(),
    )
