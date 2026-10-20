"""Deep async worker for one Civic Watch slot, from fixed catalogs through Batch seal."""
from __future__ import annotations

import asyncio
import inspect
import json
import sqlite3
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from functools import partial
from time import monotonic
from typing import Protocol

from beehive.ai.model_selection import DEFAULT_MODEL, UnsupportedModelError
from beehive.civic.ai_gateway import CivicModelGateway, verifier_model_for
from beehive.civic.ai_pipeline import CivicClaimLostError, generate_civic_candidate
from beehive.civic.clustering import (
    CivicClusteringResult,
    CivicDocumentCluster,
    cluster_civic_documents,
)
from beehive.civic.destination import (
    DESTINATION_V1_ENGINE_VERSION,
    constrain_qualification_to_destination_v1,
    is_destination_v1_claim,
)
from beehive.civic.documents import (
    CivicDocument,
    CivicFetchResult,
    CivicSourceHealth,
)
from beehive.civic.events import CivicEventLogger
from beehive.civic.evidence_refresh import EvidenceRefreshStatus, prepare_evidence_refresh
from beehive.civic.pipeline import (
    begin_civic_research,
    ingest_civic_documents,
    qualify_civic_candidate,
    resume_civic_research,
    settle_destination_subject_if_claimed,
)
from beehive.civic.production import (
    CivicHydrationResult,
    CivicHydrationStatus,
    CivicSourceProfile,
    PartyMediaDocumentHydrator,
    PartySubjectResolution,
    PartySubjectResolutionStatus,
    build_ownership_registry,
    build_source_profiles,
    construct_candidate_claim,
    event_date_for_cluster,
    filter_documents_for_vocabulary,
    persist_cluster_evidence,
    persist_research_evidence,
    resolve_candidate_party_subject,
    resolve_cluster_party_subject,
)
from beehive.civic.source_extraction import has_current_article_capture, text_sha256
from beehive.civic.source_outages import (
    OUTAGE_ESCALATION_AFTER,
    OUTAGE_HEALTH_URLS,
    OUTAGE_STATUSES,
    health_probe_transport,
    held_source_limit,
    outage_status,
    probe_health_url,
)
from beehive.civic.reasons import CivicPolicyReason
from beehive.civic.publication import (
    CivicPublicationCandidateError,
    CivicPublicationIntegrityError,
    CivicPublicationRefreshRequired,
    preflight_candidate,
)
from beehive.civic.scheduling import (
    PARTYMAP_GLOBAL_WATCH_SUBJECT,
    CivicScheduleResult,
    require_partymap_global_watch,
    schedule_partymap_civic_run,
)
from beehive.connectors.civic_sources import enabled_sources
from beehive.connectors.civic_catalog import PartyMediaSourceEntry
from beehive.connectors.civic_strategies import fetch_entry, party_media_transport
from beehive.db.civic_source_outages import observe_source_outages_if_claimed
from beehive.db.civic_batches import (
    adopt_capacity_deferrals,
    get_batch_for_run,
    get_or_create_batch_if_claimed,
    list_publication_rejections,
    refresh_candidate_for_ai_if_claimed,
    seal_batch_if_claimed,
)
from beehive.db.civic_candidates import (
    get_candidate,
    list_candidate_evidence,
    list_run_candidates,
    reject_candidate_if_claimed,
)
from beehive.db.civic_consumed_items import list_consumed_item_keys
from beehive.db.civic_evidence_captures import (
    CivicCaptureIntegrityError,
    CivicHydrationBudgetError,
    CivicSourceRefreshBlocked,
    apply_evidence_refresh_if_claimed,
    candidate_source_plan,
    complete_hydration_if_claimed,
    defer_evidence_refresh_if_claimed,
    fail_hydration_if_claimed,
    hydration_documents,
    hold_capture_integrity_if_claimed,
    reserve_evidence_refresh_if_claimed,
    reserve_hydration_if_claimed,
)
from beehive.db.civic_qualifications import get_latest_qualification
from beehive.db.civic_notifications import queue_notification
from beehive.db.civic_runs import (
    CivicRunLease,
    advance_civic_run_phase,
    claim_civic_run,
    complete_civic_run,
    get_civic_run,
    heartbeat_civic_run,
    list_pending_civic_runs,
)
from beehive.db.civic_verifications import record_research_attempt_if_claimed
from beehive.db.civic_vocabulary import (
    get_vocabulary_version,
    list_vocabulary_terms,
)
from beehive.db.civic_watches import get_civic_watch
from beehive.domain.civic import (
    CivicBatchStatus,
    CivicCandidateStatus,
    CivicResearchOutcome,
    CivicRunPhase,
    CivicRunStatus,
)

DEFAULT_LEASE_SECONDS = 300
DEFAULT_DEADLINE_SECONDS = 20 * 60
MAX_SOURCE_CONCURRENCY = 6
MAX_EVIDENCE_HYDRATIONS_PER_RUN = 25
MAX_ERROR_DETAIL_CHARS = 500


class CivicSourceExecutor(Protocol):
    def __call__(
        self, entry: object, now: datetime
    ) -> CivicFetchResult | Awaitable[CivicFetchResult]:
        ...


class CivicResearcher(Protocol):
    def __call__(
        self, claim: object, now: datetime
    ) -> Sequence[CivicDocument] | Awaitable[Sequence[CivicDocument]]:
        ...


class CivicEvidenceHydrator(Protocol):
    def __call__(
        self,
        document: CivicDocument,
        now: datetime,
    ) -> CivicHydrationResult | Awaitable[CivicHydrationResult]:
        ...


class _LeaseRenewingGateway:
    """Renew the Run lease while each individual model request is in flight."""

    def __init__(
        self,
        worker: CivicWorker,
        run_id: int,
        claim_token: str,
        *,
        watch_id: int,
        batch_id: int,
        candidate_id: int,
    ) -> None:
        self._worker = worker
        self._run_id = run_id
        self._claim_token = claim_token
        self._watch_id = watch_id
        self._batch_id = batch_id
        self._candidate_id = candidate_id

    async def complete(self, request):
        started_at = monotonic()
        row = self._worker.conn.execute(
            "SELECT attempt FROM civic_ai_stages "
            "WHERE candidate_id = ? AND run_id = ? AND stage = ? AND status = 'reserved' "
            "ORDER BY attempt DESC LIMIT 1",
            (self._candidate_id, self._run_id, request.stage.value),
        ).fetchone()
        attempt = row["attempt"] if row is not None else None
        self._worker.event_logger.emit(
            "civic.ai.stage.started",
            watch_id=self._watch_id,
            run_id=self._run_id,
            batch_id=self._batch_id,
            candidate_id=self._candidate_id,
            phase=CivicRunPhase.VERIFYING,
            status="started",
            reason=request.stage,
            model=request.model,
            attempt=attempt,
        )
        try:
            response = await self._worker._await_with_lease(
                self._worker.gateway.complete(request),
                self._run_id,
                self._claim_token,
            )
        except BaseException as exc:
            self._worker.event_logger.emit(
                "civic.ai.stage.failed",
                watch_id=self._watch_id,
                run_id=self._run_id,
                batch_id=self._batch_id,
                candidate_id=self._candidate_id,
                phase=CivicRunPhase.VERIFYING,
                status="failed",
                reason=request.stage,
                model=request.model,
                attempt=attempt,
                error_code=getattr(exc, "error_code", None) or "model_call_failed",
                error_class=type(exc).__name__,
                elapsed_ms=(monotonic() - started_at) * 1000,
            )
            raise
        self._worker.event_logger.emit(
            "civic.ai.stage.finished",
            watch_id=self._watch_id,
            run_id=self._run_id,
            batch_id=self._batch_id,
            candidate_id=self._candidate_id,
            phase=CivicRunPhase.VERIFYING,
            status="completed",
            reason=request.stage,
            model=request.model,
            attempt=attempt,
            elapsed_ms=(monotonic() - started_at) * 1000,
        )
        return response


class CivicSourceExecutionStatus(StrEnum):
    OK = "ok"
    EMPTY = "empty"
    DEGRADED = "degraded"
    TOMBSTONE = "tombstone"
    FAILED = "failed"
    # The whole site is down (listing and root both fail): held, not failed, until escalation.
    UNAVAILABLE = "unavailable"


class CivicRunExecutionStatus(StrEnum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    STALE_CLAIM = "stale_claim"


@dataclass(frozen=True)
class CivicSourceExecution:
    source_key: str
    status: CivicSourceExecutionStatus
    documents: tuple[CivicDocument, ...] = ()
    detail: str = ""

    @property
    def succeeded(self) -> bool:
        return self.status in {
            CivicSourceExecutionStatus.OK,
            CivicSourceExecutionStatus.EMPTY,
            CivicSourceExecutionStatus.DEGRADED,
        }

    @property
    def held(self) -> bool:
        return self.status is CivicSourceExecutionStatus.UNAVAILABLE


@dataclass(frozen=True)
class CivicRunExecution:
    run_id: int
    status: CivicRunExecutionStatus
    batch_id: int | None = None
    source_results: tuple[CivicSourceExecution, ...] = ()
    candidate_count: int = 0
    verified_count: int = 0
    error_code: str | None = None
    error_detail: str = ""

    @property
    def source_failures(self) -> tuple[CivicSourceExecution, ...]:
        return tuple(
            result for result in self.source_results
            if not result.succeeded and not result.held)

    @property
    def source_holds(self) -> tuple[CivicSourceExecution, ...]:
        return tuple(result for result in self.source_results if result.held)


@dataclass(frozen=True)
class CivicCycleExecution:
    schedules: tuple[CivicScheduleResult, ...]
    runs: tuple[CivicRunExecution, ...]


class _CivicWorkerClaimLost(RuntimeError):
    pass


class _CivicWorkerCancelled(RuntimeError):
    pass


async def _empty_researcher(_claim: object, _now: datetime) -> Sequence[CivicDocument]:
    # Gathering already queried every enabled application-owned source. The bounded second pass
    # is still durably spent and recorded; production never widens to an arbitrary URL/search.
    return ()


class CivicWorker:
    def __init__(
        self,
        conn: sqlite3.Connection,
        gateway: CivicModelGateway,
        *,
        source_entries: Sequence[object] | None = None,
        source_profiles: dict[str, CivicSourceProfile] | None = None,
        source_executor: CivicSourceExecutor | None = None,
        evidence_hydrator: CivicEvidenceHydrator | None = None,
        researcher: CivicResearcher | None = None,
        now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        deadline_seconds: int = DEFAULT_DEADLINE_SECONDS,
        model: str = DEFAULT_MODEL,
        verifier_model: str | None = None,
        source_concurrency: int = MAX_SOURCE_CONCURRENCY,
        global_watch_subject: str = PARTYMAP_GLOBAL_WATCH_SUBJECT,
        heartbeat_interval_seconds: float | None = None,
        event_logger: CivicEventLogger | None = None,
        site_probe: Callable[[object], int | None] | None = None,
    ) -> None:
        if lease_seconds <= 0 or deadline_seconds <= 0:
            raise ValueError("Civic worker lease/deadline must be positive")
        if source_concurrency <= 0:
            raise ValueError("Civic worker source concurrency must be positive")
        expected_verifier = verifier_model_for(model)
        if verifier_model is not None and verifier_model != expected_verifier:
            raise UnsupportedModelError(
                f"Civic verifier for {model!r} must be {expected_verifier!r}, "
                f"not {verifier_model!r}")
        heartbeat_interval = (
            lease_seconds / 3
            if heartbeat_interval_seconds is None else heartbeat_interval_seconds)
        if not 0 < heartbeat_interval < lease_seconds:
            raise ValueError(
                "Civic worker heartbeat interval must be positive and shorter than its lease")
        self.conn = conn
        self.gateway = gateway
        self.source_entries = tuple(
            source_entries if source_entries is not None else enabled_sources())
        self.profiles = (
            source_profiles if source_profiles is not None else build_source_profiles())
        self.registry = build_ownership_registry()
        self._party_media_transport = party_media_transport()
        self.source_executor = source_executor or self._fetch_source
        # The probe shares the listing fetches' crawl pacer, so it honours each site's delay.
        # An injected executor is a test double; only the real fetch path probes real sites.
        self._health_transport = health_probe_transport(self._party_media_transport.pacer)
        if site_probe is not None:
            self.site_probe = site_probe
        elif source_executor is None:
            self.site_probe = partial(probe_health_url, transport=self._health_transport)
        else:
            self.site_probe = None
        self.evidence_hydrator = evidence_hydrator or PartyMediaDocumentHydrator(
            transport=self._party_media_transport,
        )
        self.researcher = researcher or _empty_researcher
        self.now_fn = now_fn
        self.lease_seconds = lease_seconds
        self.deadline_seconds = deadline_seconds
        self.model = model
        self.verifier_model = expected_verifier
        self.global_watch_subject = global_watch_subject
        self.heartbeat_interval_seconds = heartbeat_interval
        self.event_logger = event_logger or CivicEventLogger(now_fn=now_fn)
        self._source_semaphore = asyncio.Semaphore(source_concurrency)
        self._phase_started_at: dict[int, float] = {}
        self._active_phase: dict[int, CivicRunPhase] = {}
        self._batch_ids: dict[int, int] = {}
        self._run_started_at: dict[int, float] = {}

    async def run_due_cycle(self, *, pending_limit: int = 20) -> CivicCycleExecution:
        started_at = monotonic()
        now = self.now_fn()
        self.event_logger.emit(
            "civic.cycle.started", phase="scheduling", status="started")
        self.event_logger.emit(
            "civic.schedule.started", phase="scheduling", status="started")
        try:
            schedules = schedule_partymap_civic_run(
                self.conn,
                now,
                now_fn=self.now_fn,
                subject=self.global_watch_subject,
            )
        except Exception as exc:
            self.event_logger.emit(
                "civic.schedule.failed",
                phase="scheduling",
                status="failed",
                error_code="schedule_failed",
                error_class=type(exc).__name__,
                elapsed_ms=(monotonic() - started_at) * 1000,
            )
            self.event_logger.emit(
                "civic.cycle.failed",
                phase="scheduling",
                status="failed",
                error_code="schedule_failed",
                error_class=type(exc).__name__,
                elapsed_ms=(monotonic() - started_at) * 1000,
            )
            raise
        for schedule in schedules:
            self.event_logger.emit(
                "civic.schedule.finished",
                watch_id=schedule.watch.id,
                run_id=(
                    schedule.run.id
                    if schedule.run is not None
                    else schedule.blocking_run.id
                    if schedule.blocking_run is not None
                    else None
                ),
                phase="scheduling",
                status=schedule.disposition,
                elapsed_ms=(monotonic() - started_at) * 1000,
            )
        global_watch_id = schedules[0].watch.id
        executions: list[CivicRunExecution] = []
        for run in list_pending_civic_runs(self.conn, pending_limit):
            if run.watch_id != global_watch_id:
                continue
            claim_started_at = monotonic()
            self.event_logger.emit(
                "civic.run.claim.started",
                watch_id=run.watch_id,
                run_id=run.id,
                phase=CivicRunPhase.GATHERING,
                status="started",
            )
            try:
                lease = claim_civic_run(
                    self.conn,
                    run.id,
                    self.now_fn(),
                    self.lease_seconds,
                    self.deadline_seconds,
                    now_fn=self.now_fn,
                )
            except Exception as exc:
                self.event_logger.emit(
                    "civic.run.claim.failed",
                    watch_id=run.watch_id,
                    run_id=run.id,
                    phase=CivicRunPhase.GATHERING,
                    status="failed",
                    error_code="run_claim_failed",
                    error_class=type(exc).__name__,
                    elapsed_ms=(monotonic() - claim_started_at) * 1000,
                )
                self.event_logger.emit(
                    "civic.cycle.failed",
                    watch_id=global_watch_id,
                    run_id=run.id,
                    phase="scheduling",
                    status="failed",
                    error_code="run_claim_failed",
                    error_class=type(exc).__name__,
                    elapsed_ms=(monotonic() - started_at) * 1000,
                )
                raise
            if lease is None:
                self.event_logger.emit(
                    "civic.run.claim.finished",
                    watch_id=run.watch_id,
                    run_id=run.id,
                    phase=CivicRunPhase.GATHERING,
                    status="skipped",
                    elapsed_ms=(monotonic() - claim_started_at) * 1000,
                )
                continue
            self.event_logger.emit(
                "civic.run.claim.finished",
                watch_id=lease.run.watch_id,
                run_id=lease.run.id,
                phase=lease.run.phase,
                status="claimed",
                attempt_count=lease.attempt_count,
                lease_expires_at=lease.lease_expires_at,
                deadline_at=lease.run.deadline_at,
                elapsed_ms=(monotonic() - claim_started_at) * 1000,
            )
            try:
                executions.append(await self._execute_claimed(lease))
            except BaseException as exc:
                self.event_logger.emit(
                    "civic.cycle.failed",
                    watch_id=global_watch_id,
                    run_id=run.id,
                    phase=lease.run.phase,
                    status="failed",
                    error_code="run_execution_interrupted",
                    error_class=type(exc).__name__,
                    elapsed_ms=(monotonic() - started_at) * 1000,
                )
                raise
        cycle = CivicCycleExecution(schedules=schedules, runs=tuple(executions))
        failed_count = sum(
            result.status is not CivicRunExecutionStatus.COMPLETED
            or bool(result.source_failures)
            for result in executions
        )
        self.event_logger.emit(
            "civic.cycle.finished",
            watch_id=global_watch_id,
            phase="scheduling",
            status="completed" if failed_count == 0 else "degraded",
            scheduled_count=len(schedules),
            run_count=len(executions),
            failed_count=failed_count,
            elapsed_ms=(monotonic() - started_at) * 1000,
        )
        return cycle

    async def execute_run(self, run_id: int) -> CivicRunExecution | None:
        claim_started_at = monotonic()
        self.event_logger.emit(
            "civic.run.claim.started",
            run_id=run_id,
            phase=CivicRunPhase.GATHERING,
            status="started",
        )
        try:
            lease = claim_civic_run(
                self.conn,
                run_id,
                self.now_fn(),
                self.lease_seconds,
                self.deadline_seconds,
                now_fn=self.now_fn,
            )
        except Exception as exc:
            self.event_logger.emit(
                "civic.run.claim.failed",
                run_id=run_id,
                phase=CivicRunPhase.GATHERING,
                status="failed",
                error_code="run_claim_failed",
                error_class=type(exc).__name__,
                elapsed_ms=(monotonic() - claim_started_at) * 1000,
            )
            raise
        self.event_logger.emit(
            "civic.run.claim.finished",
            run_id=run_id,
            watch_id=lease.run.watch_id if lease is not None else None,
            phase=lease.run.phase if lease is not None else CivicRunPhase.GATHERING,
            status="claimed" if lease is not None else "skipped",
            attempt_count=lease.attempt_count if lease is not None else None,
            lease_expires_at=lease.lease_expires_at if lease is not None else None,
            deadline_at=lease.run.deadline_at if lease is not None else None,
            elapsed_ms=(monotonic() - claim_started_at) * 1000,
        )
        return None if lease is None else await self._execute_claimed(lease)

    async def _execute_claimed(self, lease: CivicRunLease) -> CivicRunExecution:
        run = lease.run
        token = run.claim_token
        source_results: tuple[CivicSourceExecution, ...] = ()
        self._run_started_at[run.id] = monotonic()
        self.event_logger.emit(
            "civic.run.started",
            watch_id=run.watch_id,
            run_id=run.id,
            phase=run.phase or CivicRunPhase.GATHERING,
            status=run.status,
            attempt_count=lease.attempt_count,
            lease_expires_at=lease.lease_expires_at,
            deadline_at=run.deadline_at,
        )
        self._start_phase(run.id, run.watch_id, run.phase or CivicRunPhase.GATHERING)
        try:
            watch = get_civic_watch(self.conn, run.watch_id)
            if watch is None:
                raise ValueError(f"no Civic Watch with id={run.watch_id}")
            global_watch = require_partymap_global_watch(
                self.conn, subject=self.global_watch_subject)
            if watch.id != global_watch.id:
                raise ValueError(
                    f"Civic Run {run.id} does not belong to the configured global PartyMap "
                    f"Watch {global_watch.id}")
            vocabulary = (
                get_vocabulary_version(self.conn, run.vocabulary_version_id)
                if run.vocabulary_version_id is not None else None
            )
            if vocabulary is None:
                return self._fail(
                    run.id, token, "missing_vocabulary",
                    "the Civic Run has no pinned published vocabulary")
            terms = list_vocabulary_terms(self.conn, vocabulary.id)
            batch = get_or_create_batch_if_claimed(
                self.conn,
                run.id,
                token,
                self.now_fn(),
                now_fn=self.now_fn,
            )
            if batch is None:
                raise _CivicWorkerClaimLost
            self._batch_ids[run.id] = batch.id
            self.event_logger.emit(
                "civic.batch.building",
                watch_id=watch.id,
                run_id=run.id,
                batch_id=batch.id,
                idempotency_key=batch.idempotency_key,
                phase=run.phase or CivicRunPhase.GATHERING,
                status=batch.status,
            )

            if batch.status is CivicBatchStatus.SEALED:
                self._advance(run.id, token, CivicRunPhase.PUBLISHING)
                self._complete(run.id, token)
                result = CivicRunExecution(
                    run_id=run.id,
                    status=CivicRunExecutionStatus.COMPLETED,
                    batch_id=batch.id,
                    candidate_count=batch.candidate_count,
                    verified_count=batch.candidate_count,
                )
                self._finish_run(watch.id, result)
                return result
            if batch.status is not CivicBatchStatus.BUILDING:
                raise ValueError(
                    f"Civic Batch {batch.id} is {batch.status.value}; a Run may resume only a "
                    "building or sealed Batch")

            gathered = await self._await_with_lease(
                self._gather_sources(run.id, watch.id, batch.id), run.id, token)
            self._require_claim(run.id, token)
            source_results = self._apply_outage_policy(
                gathered, run_id=run.id, claim_token=token,
                watch_id=watch.id, batch_id=batch.id)
            failures = tuple(
                result for result in source_results
                if not result.succeeded and not result.held)
            holds = tuple(result for result in source_results if result.held)
            succeeded_count = sum(result.succeeded for result in source_results)
            self.event_logger.emit(
                "civic.source.aggregate",
                watch_id=watch.id,
                run_id=run.id,
                batch_id=batch.id,
                phase=CivicRunPhase.GATHERING,
                status=(
                    "failed"
                    if succeeded_count == 0
                    else "degraded" if failures or holds else "completed"
                ),
                source_count=len(source_results),
                succeeded_count=succeeded_count,
                failed_count=len(failures),
                held_count=len(holds),
                document_count=sum(
                    len(result.documents) for result in source_results if result.succeeded
                ),
            )
            if failures or holds:
                self._record_source_failures(run.id, watch.id, failures + holds)
            if not any(result.succeeded for result in source_results):
                detail = "; ".join(
                    f"{result.source_key}: {result.detail or result.status.value}"
                    for result in failures)
                return self._fail(
                    run.id, token, "all_civic_sources_failed", detail,
                    source_results=source_results)

            if watch.subject == PARTYMAP_GLOBAL_WATCH_SUBJECT:
                adoption = adopt_capacity_deferrals(
                    self.conn, batch.id, run.id, token, self.now_fn(), now_fn=self.now_fn)
                if adoption is None:
                    raise _CivicWorkerClaimLost
                for candidate_id in adoption.adopted_candidate_ids:
                    self.event_logger.emit(
                        "civic.candidate.ingested", watch_id=watch.id, run_id=run.id,
                        batch_id=batch.id, candidate_id=candidate_id,
                        phase=CivicRunPhase.GATHERING, status="adopted")
                await self._refresh_run_evidence(run.id, token, watch.id, batch.id)

            documents = tuple(
                document
                for result in source_results if result.succeeded
                for document in result.documents
            )
            scoped = filter_documents_for_vocabulary(documents, terms)
            clustering, subject_resolutions, hydration_deferred, cluster_overflow = (
                await self._prepare_candidate_documents(
                    scoped,
                    watch_id=watch.id,
                    run_id=run.id,
                    batch_id=batch.id,
                    claim_token=token,
                )
            )
            if hydration_deferred:
                queue_notification(
                    self.conn,
                    watch.id,
                    "civic_detail_hydration_deferred",
                    f"civic-run:{run.id}:detail-hydration-deferred",
                    f"Civic Run {run.id} detail hydration deferred",
                    self.now_fn(),
                    run_id=run.id,
                    body=(
                        f"{hydration_deferred} metadata-only party clusters remain "
                        "unconsumed for a later run"
                    ),
                )
            ingest_started_at = monotonic()
            self.event_logger.emit(
                "civic.ingest.started",
                watch_id=watch.id,
                run_id=run.id,
                batch_id=batch.id,
                phase=CivicRunPhase.GATHERING,
                status="started",
                document_count=len(scoped),
            )
            try:
                ingest = ingest_civic_documents(
                    self.conn,
                    run_id=run.id,
                    claim_token=token,
                    documents=scoped,
                    now=self.now_fn(),
                    now_fn=self.now_fn,
                    clustering=clustering,
                )
            except Exception as exc:
                self.event_logger.emit(
                    "civic.ingest.failed",
                    watch_id=watch.id,
                    run_id=run.id,
                    batch_id=batch.id,
                    phase=CivicRunPhase.GATHERING,
                    status="failed",
                    error_code="candidate_ingest_failed",
                    error_class=type(exc).__name__,
                    elapsed_ms=(monotonic() - ingest_started_at) * 1000,
                )
                raise
            if ingest is None:
                raise _CivicWorkerClaimLost
            for candidate in ingest.created_candidates:
                self.event_logger.emit(
                    "civic.candidate.ingested",
                    watch_id=watch.id,
                    run_id=run.id,
                    batch_id=batch.id,
                    candidate_id=candidate.id,
                    phase=CivicRunPhase.GATHERING,
                    status="created",
                )
            for candidate in ingest.reused_candidates:
                self.event_logger.emit(
                    "civic.candidate.ingested",
                    watch_id=watch.id,
                    run_id=run.id,
                    batch_id=batch.id,
                    candidate_id=candidate.id,
                    phase=CivicRunPhase.GATHERING,
                    status="reused",
                )
            for candidate_id in ingest.adopted_candidate_ids:
                self.event_logger.emit(
                    "civic.candidate.ingested",
                    watch_id=watch.id,
                    run_id=run.id,
                    batch_id=batch.id,
                    candidate_id=candidate_id,
                    phase=CivicRunPhase.GATHERING,
                    status="adopted",
                )
            self.event_logger.emit(
                "civic.ingest.finished",
                watch_id=watch.id,
                run_id=run.id,
                batch_id=batch.id,
                phase=CivicRunPhase.GATHERING,
                status="completed",
                candidate_count=len(list_run_candidates(self.conn, run.id)),
                created_count=len(ingest.created_candidates),
                replayed_count=len(ingest.reused_candidates),
                deferred_count=len(ingest.deferred_clusters),
                skipped_count=len(ingest.skipped_item_keys),
                elapsed_ms=(monotonic() - ingest_started_at) * 1000,
            )
            if cluster_overflow:
                queue_notification(
                    self.conn,
                    watch.id,
                    "civic_cluster_overflow",
                    f"civic-run:{run.id}:cluster-overflow",
                    f"Civic Run {run.id} cluster limit reached",
                    self.now_fn(),
                    run_id=run.id,
                    body="\n".join(
                        cluster.cluster_key for cluster in cluster_overflow)[:4000],
                )
            candidates_by_key = {
                candidate.dedupe_key: candidate
                for candidate in ingest.candidates
                if candidate.run_id == run.id
            }
            for cluster in ingest.clustering.clusters:
                self._require_claim(run.id, token)
                candidate = candidates_by_key.get(cluster.cluster_key)
                if candidate is None:
                    self._require_claim(run.id, token)
                    continue
                subject_started_at = monotonic()
                self.event_logger.emit(
                    "civic.candidate.subject.started",
                    watch_id=watch.id,
                    run_id=run.id,
                    batch_id=batch.id,
                    candidate_id=candidate.id,
                    phase=CivicRunPhase.GATHERING,
                    status="started",
                )
                try:
                    resolution = subject_resolutions.get(cluster.cluster_key)
                    if resolution is None:
                        resolution = resolve_cluster_party_subject(cluster, self.profiles)
                    written = persist_cluster_evidence(
                        self.conn,
                        run_id=run.id,
                        claim_token=token,
                        candidate=candidate,
                        cluster=cluster,
                        watch=watch,
                        subject_id=resolution.subject_id or "",
                        profiles=self.profiles,
                        now=self.now_fn(),
                        now_fn=self.now_fn,
                    )
                except Exception as exc:
                    self.event_logger.emit(
                        "civic.candidate.subject.failed",
                        watch_id=watch.id,
                        run_id=run.id,
                        batch_id=batch.id,
                        candidate_id=candidate.id,
                        phase=CivicRunPhase.GATHERING,
                        status="failed",
                        error_code="subject_resolution_failed",
                        error_class=type(exc).__name__,
                        elapsed_ms=(monotonic() - subject_started_at) * 1000,
                    )
                    raise
                if written is None:
                    raise _CivicWorkerClaimLost
                subject_output = None
                if not resolution.is_resolved:
                    subject_output = self._settle_subject_resolution(
                        run.id, token, candidate.id, resolution)
                self.event_logger.emit(
                    "civic.candidate.subject.finished",
                    watch_id=watch.id,
                    run_id=run.id,
                    batch_id=batch.id,
                    candidate_id=candidate.id,
                    phase=CivicRunPhase.GATHERING,
                    status=resolution.status,
                    reason=(
                        None if resolution.is_resolved else resolution.status.value
                    ),
                    evidence_count=len(written),
                    elapsed_ms=(monotonic() - subject_started_at) * 1000,
                )
                if subject_output is not None:
                    self._emit_qualification(
                        watch.id,
                        run.id,
                        batch.id,
                        candidate.id,
                        CivicRunPhase.GATHERING,
                        subject_output,
                        subject_started_at,
                    )
                self._require_claim(run.id, token)

            if watch.subject == PARTYMAP_GLOBAL_WATCH_SUBJECT:
                await self._refresh_run_evidence(run.id, token, watch.id, batch.id)

            for candidate in list_run_candidates(self.conn, run.id):
                self._require_claim(run.id, token)
                if (watch.subject != PARTYMAP_GLOBAL_WATCH_SUBJECT
                        or candidate.status is not CivicCandidateStatus.VERIFIED):
                    continue
                try:
                    preflight_candidate(self.conn, candidate.id)
                except CivicCaptureIntegrityError:
                    self._hold_corrupt_capture(
                        candidate.id, run.id, token, watch.id, batch.id)
                except CivicSourceRefreshBlocked as exc:
                    self._hold_unready_source(
                        candidate.id, run.id, token, watch.id, batch.id, exc.reason)
                except CivicPublicationRefreshRequired as exc:
                    refreshed = refresh_candidate_for_ai_if_claimed(
                        self.conn, candidate_id=candidate.id, run_id=run.id,
                        claim_token=token, expected_revision=candidate.ai_revision,
                        reason=exc.reason, now=self.now_fn(), now_fn=self.now_fn)
                    if refreshed is None:
                        raise _CivicWorkerClaimLost
                    self.event_logger.emit(
                        "civic.candidate.ai.refresh",
                        watch_id=watch.id, run_id=run.id, batch_id=batch.id,
                        candidate_id=candidate.id, phase=CivicRunPhase.GATHERING,
                        status=refreshed.status,
                        reason=refreshed.rejection_code or exc.reason,
                        attempt=refreshed.ai_revision,
                    )
                except CivicPublicationCandidateError:
                    # Genuine invalid content is settled by the final publication preflight.
                    continue
                except CivicPublicationIntegrityError as exc:
                    # Keep the final seal fail-closed, but first let fresh work settle so a
                    # corrupt carryover cannot make Run failure discard newly consumed items.
                    self.event_logger.emit(
                        "civic.candidate.preflight.failed",
                        watch_id=watch.id, run_id=run.id, batch_id=batch.id,
                        candidate_id=candidate.id, phase=CivicRunPhase.GATHERING,
                        status="integrity_error", error_code="publication_provenance_corrupt",
                        error_class=type(exc).__name__)

            self._advance(run.id, token, CivicRunPhase.SCREENING)
            for candidate in list_run_candidates(self.conn, run.id):
                self._require_claim(run.id, token)
                if candidate.status is not CivicCandidateStatus.DRAFTED:
                    self._require_claim(run.id, token)
                    continue
                subject_started_at = monotonic()
                self.event_logger.emit(
                    "civic.candidate.subject.started",
                    watch_id=watch.id,
                    run_id=run.id,
                    batch_id=batch.id,
                    candidate_id=candidate.id,
                    phase=CivicRunPhase.SCREENING,
                    status="started",
                )
                try:
                    resolution = resolve_candidate_party_subject(
                        list_candidate_evidence(self.conn, candidate.id), self.profiles)
                except Exception as exc:
                    self.event_logger.emit(
                        "civic.candidate.subject.failed",
                        watch_id=watch.id,
                        run_id=run.id,
                        batch_id=batch.id,
                        candidate_id=candidate.id,
                        phase=CivicRunPhase.SCREENING,
                        status="failed",
                        error_code="subject_resolution_failed",
                        error_class=type(exc).__name__,
                        elapsed_ms=(monotonic() - subject_started_at) * 1000,
                    )
                    raise
                if not resolution.is_resolved:
                    output = self._settle_subject_resolution(
                        run.id, token, candidate.id, resolution)
                    self.event_logger.emit(
                        "civic.candidate.subject.finished",
                        watch_id=watch.id,
                        run_id=run.id,
                        batch_id=batch.id,
                        candidate_id=candidate.id,
                        phase=CivicRunPhase.SCREENING,
                        status=resolution.status,
                        reason=resolution.status,
                        elapsed_ms=(monotonic() - subject_started_at) * 1000,
                    )
                    self._emit_qualification(
                        watch.id,
                        run.id,
                        batch.id,
                        candidate.id,
                        CivicRunPhase.SCREENING,
                        output,
                        subject_started_at,
                    )
                    self._require_claim(run.id, token)
                    continue
                self.event_logger.emit(
                    "civic.candidate.subject.finished",
                    watch_id=watch.id,
                    run_id=run.id,
                    batch_id=batch.id,
                    candidate_id=candidate.id,
                    phase=CivicRunPhase.SCREENING,
                    status=resolution.status,
                    elapsed_ms=(monotonic() - subject_started_at) * 1000,
                )
                qualification_started_at = monotonic()
                self.event_logger.emit(
                    "civic.candidate.qualification.started",
                    watch_id=watch.id,
                    run_id=run.id,
                    batch_id=batch.id,
                    candidate_id=candidate.id,
                    phase=CivicRunPhase.SCREENING,
                    status="started",
                )
                try:
                    claim = construct_candidate_claim(self.conn, candidate)
                except ValueError:
                    if candidate.ai_revision == 0:
                        raise
                    rejected = reject_candidate_if_claimed(
                        self.conn, run_id=run.id, claim_token=token,
                        candidate_id=candidate.id, rejection_code="refresh_requalification_invalid",
                        now=self.now_fn(), now_fn=self.now_fn)
                    if rejected is None:
                        raise _CivicWorkerClaimLost
                    self.event_logger.emit(
                        "civic.candidate.qualification.finished",
                        watch_id=watch.id, run_id=run.id, batch_id=batch.id,
                        candidate_id=candidate.id, phase=CivicRunPhase.SCREENING,
                        status="rejected", reason="refresh_requalification_invalid",
                        elapsed_ms=(monotonic() - qualification_started_at) * 1000)
                    continue
                try:
                    output = qualify_civic_candidate(
                        self.conn,
                        run_id=run.id,
                        claim_token=token,
                        claim=claim,
                        registry=self.registry,
                        now=self.now_fn(),
                        now_fn=self.now_fn,
                        engine_version=DESTINATION_V1_ENGINE_VERSION,
                        qualification_scope=constrain_qualification_to_destination_v1,
                    )
                except CivicCaptureIntegrityError:
                    self._hold_corrupt_capture(
                        candidate.id, run.id, token, watch.id, batch.id)
                    continue
                except CivicSourceRefreshBlocked as exc:
                    self._hold_unready_source(
                        candidate.id, run.id, token, watch.id, batch.id, exc.reason)
                    continue
                except Exception as exc:
                    self.event_logger.emit(
                        "civic.candidate.qualification.failed",
                        watch_id=watch.id,
                        run_id=run.id,
                        batch_id=batch.id,
                        candidate_id=candidate.id,
                        phase=CivicRunPhase.SCREENING,
                        status="failed",
                        error_code="qualification_failed",
                        error_class=type(exc).__name__,
                        elapsed_ms=(monotonic() - qualification_started_at) * 1000,
                    )
                    raise
                if output is None:
                    raise _CivicWorkerClaimLost
                self._emit_qualification(
                    watch.id,
                    run.id,
                    batch.id,
                    candidate.id,
                    CivicRunPhase.SCREENING,
                    output,
                    qualification_started_at,
                )
                self._require_claim(run.id, token)

            self._advance(run.id, token, CivicRunPhase.RESEARCHING)
            research_errors: list[str] = []
            for candidate in list_run_candidates(self.conn, run.id):
                self._require_claim(run.id, token)
                if candidate.status is not CivicCandidateStatus.RESEARCHING:
                    self._require_claim(run.id, token)
                    continue
                claim = construct_candidate_claim(self.conn, candidate)
                attempt = resume_civic_research(self.conn, candidate.id)
                if attempt is None:
                    attempt = begin_civic_research(
                        self.conn,
                        run_id=run.id,
                        claim_token=token,
                        candidate_id=candidate.id,
                        now=self.now_fn(),
                        now_fn=self.now_fn,
                    )
                if attempt is not None:
                    started_at = self.now_fn()
                    research_started_at = monotonic()
                    self.event_logger.emit(
                        "civic.candidate.research.started",
                        watch_id=watch.id,
                        run_id=run.id,
                        batch_id=batch.id,
                        candidate_id=candidate.id,
                        phase=CivicRunPhase.RESEARCHING,
                        status="started",
                        attempt=attempt,
                    )
                    research_error: Exception | None = None
                    written = ()
                    try:
                        extra = tuple(await self._await_with_lease(
                            self._call_researcher(claim, started_at),
                            run.id,
                            token,
                        ))
                        persisted = persist_research_evidence(
                            self.conn,
                            run_id=run.id,
                            claim_token=token,
                            candidate=candidate,
                            claim=claim,
                            documents=extra,
                            profiles=self.profiles,
                            now=self.now_fn(),
                            now_fn=self.now_fn,
                        )
                        if persisted is None:
                            raise _CivicWorkerClaimLost
                        written = persisted
                    except _CivicWorkerClaimLost:
                        raise
                    except Exception as exc:
                        research_error = exc

                    self._require_claim(run.id, token)
                    if research_error is None:
                        research_outcome = (
                            CivicResearchOutcome.RESOLVED
                            if written else CivicResearchOutcome.UNRESOLVED
                        )
                        recorded = record_research_attempt_if_claimed(
                            self.conn,
                            run_id=run.id,
                            claim_token=token,
                            candidate_id=candidate.id,
                            attempt_number=attempt,
                            outcome=research_outcome,
                            started_at=started_at,
                            now=self.now_fn(),
                            now_fn=self.now_fn,
                            evidence_added=len(written),
                            notes="bounded catalog research",
                        )
                    else:
                        detail = (
                            f"{type(research_error).__name__}: {research_error}"
                        )[:MAX_ERROR_DETAIL_CHARS]
                        recorded = record_research_attempt_if_claimed(
                            self.conn,
                            run_id=run.id,
                            claim_token=token,
                            candidate_id=candidate.id,
                            attempt_number=attempt,
                            outcome=CivicResearchOutcome.ERROR,
                            started_at=started_at,
                            now=self.now_fn(),
                            now_fn=self.now_fn,
                            error_code="research_failed",
                            notes=detail,
                        )
                        research_errors.append(
                            f"Candidate {candidate.id}: {detail}")
                    if recorded is None:
                        raise _CivicWorkerClaimLost
                    if research_error is None:
                        self.event_logger.emit(
                            "civic.candidate.research.finished",
                            watch_id=watch.id,
                            run_id=run.id,
                            batch_id=batch.id,
                            candidate_id=candidate.id,
                            phase=CivicRunPhase.RESEARCHING,
                            status=research_outcome,
                            attempt=attempt,
                            evidence_count=len(written),
                            elapsed_ms=(monotonic() - research_started_at) * 1000,
                        )
                    else:
                        self.event_logger.emit(
                            "civic.candidate.research.failed",
                            watch_id=watch.id,
                            run_id=run.id,
                            batch_id=batch.id,
                            candidate_id=candidate.id,
                            phase=CivicRunPhase.RESEARCHING,
                            status="error",
                            attempt=attempt,
                            error_code="research_failed",
                            error_class=type(research_error).__name__,
                            elapsed_ms=(monotonic() - research_started_at) * 1000,
                        )

                if watch.subject == PARTYMAP_GLOBAL_WATCH_SUBJECT:
                    await self._refresh_run_evidence(
                        run.id, token, watch.id, batch.id, candidate_ids=(candidate.id,))
                    current = get_candidate(self.conn, candidate.id)
                    if current is None:
                        raise _CivicWorkerClaimLost
                    if current.status not in {
                        CivicCandidateStatus.DRAFTED, CivicCandidateStatus.RESEARCHING,
                        CivicCandidateStatus.VERIFYING,
                    }:
                        continue
                    claim = construct_candidate_claim(self.conn, current)

                qualification_started_at = monotonic()
                self.event_logger.emit(
                    "civic.candidate.qualification.started",
                    watch_id=watch.id,
                    run_id=run.id,
                    batch_id=batch.id,
                    candidate_id=candidate.id,
                    phase=CivicRunPhase.RESEARCHING,
                    status="started",
                    attempt=attempt,
                )
                try:
                    output = qualify_civic_candidate(
                        self.conn,
                        run_id=run.id,
                        claim_token=token,
                        claim=claim,
                        registry=self.registry,
                        now=self.now_fn(),
                        now_fn=self.now_fn,
                        engine_version=DESTINATION_V1_ENGINE_VERSION,
                        qualification_scope=constrain_qualification_to_destination_v1,
                    )
                except CivicCaptureIntegrityError:
                    self._hold_corrupt_capture(
                        candidate.id, run.id, token, watch.id, batch.id)
                    continue
                except CivicSourceRefreshBlocked as exc:
                    self._hold_unready_source(
                        candidate.id, run.id, token, watch.id, batch.id, exc.reason)
                    continue
                except Exception as exc:
                    self.event_logger.emit(
                        "civic.candidate.qualification.failed",
                        watch_id=watch.id,
                        run_id=run.id,
                        batch_id=batch.id,
                        candidate_id=candidate.id,
                        phase=CivicRunPhase.RESEARCHING,
                        status="failed",
                        error_code="qualification_failed",
                        error_class=type(exc).__name__,
                        elapsed_ms=(monotonic() - qualification_started_at) * 1000,
                    )
                    raise
                if output is None:
                    raise _CivicWorkerClaimLost
                self._emit_qualification(
                    watch.id,
                    run.id,
                    batch.id,
                    candidate.id,
                    CivicRunPhase.RESEARCHING,
                    output,
                    qualification_started_at,
                )
                self._require_claim(run.id, token)

            self._advance(run.id, token, CivicRunPhase.VERIFYING)
            verified_count = 0
            for candidate in list_run_candidates(self.conn, run.id):
                self._require_claim(run.id, token)
                if candidate.status is not CivicCandidateStatus.VERIFYING:
                    self._require_claim(run.id, token)
                    continue
                ai_started_at = monotonic()
                self.event_logger.emit(
                    "civic.candidate.ai.started",
                    watch_id=watch.id,
                    run_id=run.id,
                    batch_id=batch.id,
                    candidate_id=candidate.id,
                    phase=CivicRunPhase.VERIFYING,
                    status="started",
                    model=self.model,
                )
                try:
                    claim = construct_candidate_claim(self.conn, candidate)
                except ValueError:
                    rejected = reject_candidate_if_claimed(
                        self.conn,
                        run_id=run.id,
                        claim_token=token,
                        candidate_id=candidate.id,
                        rejection_code="destination_scope_v1",
                        now=self.now_fn(),
                        now_fn=self.now_fn,
                    )
                    if rejected is None:
                        raise _CivicWorkerClaimLost
                    self.event_logger.emit(
                        "civic.candidate.ai.finished",
                        watch_id=watch.id,
                        run_id=run.id,
                        batch_id=batch.id,
                        candidate_id=candidate.id,
                        phase=CivicRunPhase.VERIFYING,
                        status="rejected",
                        reason="destination_scope_v1",
                        model=self.model,
                        ai_calls=0,
                        elapsed_ms=(monotonic() - ai_started_at) * 1000,
                    )
                    self._require_claim(run.id, token)
                    continue
                qualification = get_latest_qualification(self.conn, candidate.id)
                if (qualification is None
                        or qualification.engine_version != DESTINATION_V1_ENGINE_VERSION
                        or not is_destination_v1_claim(claim)):
                    rejected = reject_candidate_if_claimed(
                        self.conn,
                        run_id=run.id,
                        claim_token=token,
                        candidate_id=candidate.id,
                        rejection_code="destination_scope_v1",
                        now=self.now_fn(),
                        now_fn=self.now_fn,
                    )
                    if rejected is None:
                        raise _CivicWorkerClaimLost
                    self.event_logger.emit(
                        "civic.candidate.ai.finished",
                        watch_id=watch.id,
                        run_id=run.id,
                        batch_id=batch.id,
                        candidate_id=candidate.id,
                        phase=CivicRunPhase.VERIFYING,
                        status="rejected",
                        reason="destination_scope_v1",
                        model=self.model,
                        ai_calls=0,
                        elapsed_ms=(monotonic() - ai_started_at) * 1000,
                    )
                    self._require_claim(run.id, token)
                    continue
                try:
                    lease_gateway = _LeaseRenewingGateway(
                        self,
                        run.id,
                        token,
                        watch_id=watch.id,
                        batch_id=batch.id,
                        candidate_id=candidate.id,
                    )
                    result = await generate_civic_candidate(
                        self.conn,
                        run_id=run.id,
                        claim_token=token,
                        candidate_id=candidate.id,
                        gateway=lease_gateway,
                        now=self.now_fn(),
                        now_fn=self.now_fn,
                        model=self.model,
                        verifier_model=self.verifier_model,
                    )
                except CivicCaptureIntegrityError:
                    self._hold_corrupt_capture(
                        candidate.id, run.id, token, watch.id, batch.id)
                    continue
                except CivicSourceRefreshBlocked as exc:
                    self._hold_unready_source(
                        candidate.id, run.id, token, watch.id, batch.id, exc.reason)
                    continue
                except BaseException as exc:
                    self.event_logger.emit(
                        "civic.candidate.ai.failed",
                        watch_id=watch.id,
                        run_id=run.id,
                        batch_id=batch.id,
                        candidate_id=candidate.id,
                        phase=CivicRunPhase.VERIFYING,
                        status="failed",
                        error_code="ai_pipeline_failed",
                        error_class=type(exc).__name__,
                        elapsed_ms=(monotonic() - ai_started_at) * 1000,
                    )
                    raise
                self.event_logger.emit(
                    "civic.candidate.ai.finished",
                    watch_id=watch.id,
                    run_id=run.id,
                    batch_id=batch.id,
                    candidate_id=candidate.id,
                    phase=CivicRunPhase.VERIFYING,
                    status=(
                        "verified"
                        if result.verified
                        else "deferred" if result.is_deferred else "rejected"
                    ),
                    reason=result.deferral_reason or result.rejection_code,
                    model=self.model,
                    ai_calls=result.ai_calls_made,
                    replayed_count=len(result.replayed_stages),
                    elapsed_ms=(monotonic() - ai_started_at) * 1000,
                )
                if result.verified:
                    verified_count += 1
                self._require_claim(run.id, token)

            self._advance(run.id, token, CivicRunPhase.PUBLISHING)
            seal_started_at = monotonic()
            self.event_logger.emit(
                "civic.batch.seal.started",
                watch_id=watch.id,
                run_id=run.id,
                batch_id=batch.id,
                idempotency_key=batch.idempotency_key,
                phase=CivicRunPhase.PUBLISHING,
                status="started",
            )
            try:
                sealed = seal_batch_if_claimed(
                    self.conn,
                    batch.id,
                    run.id,
                    token,
                    self.now_fn(),
                    now_fn=self.now_fn,
                )
            except Exception as exc:
                self.event_logger.emit(
                    "civic.batch.seal.failed",
                    watch_id=watch.id,
                    run_id=run.id,
                    batch_id=batch.id,
                    idempotency_key=batch.idempotency_key,
                    phase=CivicRunPhase.PUBLISHING,
                    status="failed",
                    error_code="batch_seal_failed",
                    error_class=type(exc).__name__,
                    elapsed_ms=(monotonic() - seal_started_at) * 1000,
                )
                raise
            if sealed is None:
                raise _CivicWorkerClaimLost
            for rejection in list_publication_rejections(self.conn, sealed.id):
                self.event_logger.emit(
                    "civic.candidate.publication.rejected",
                    watch_id=watch.id,
                    run_id=run.id,
                    batch_id=sealed.id,
                    candidate_id=rejection.candidate_id,
                    phase=CivicRunPhase.PUBLISHING,
                    status="rejected",
                    reason=rejection.reason,
                )
            self.event_logger.emit(
                "civic.batch.seal.finished",
                watch_id=watch.id,
                run_id=run.id,
                batch_id=sealed.id,
                idempotency_key=sealed.idempotency_key,
                content_hash=sealed.content_hash,
                phase=CivicRunPhase.PUBLISHING,
                status=sealed.status,
                candidate_count=sealed.candidate_count,
                elapsed_ms=(monotonic() - seal_started_at) * 1000,
            )
            self._complete(run.id, token)
            result = CivicRunExecution(
                run_id=run.id,
                status=CivicRunExecutionStatus.COMPLETED,
                batch_id=sealed.id,
                source_results=source_results,
                candidate_count=len(list_run_candidates(self.conn, run.id)),
                verified_count=verified_count,
                error_detail="; ".join(research_errors),
            )
            self._finish_run(watch.id, result)
            return result
        except (_CivicWorkerClaimLost, CivicClaimLostError):
            batch = get_batch_for_run(self.conn, run.id)
            self._fail_phase(
                run.id, run.watch_id, "claim_lost", "stale_claim")
            result = CivicRunExecution(
                run_id=run.id,
                status=CivicRunExecutionStatus.STALE_CLAIM,
                batch_id=batch.id if batch else None,
                source_results=source_results,
                error_code="claim_lost",
            )
            self._finish_run(run.watch_id, result)
            return result
        except _CivicWorkerCancelled:
            batch = get_batch_for_run(self.conn, run.id)
            self._finish_phase(run.id, run.watch_id, status="cancelled")
            result = CivicRunExecution(
                run_id=run.id,
                status=CivicRunExecutionStatus.CANCELLED,
                batch_id=batch.id if batch else None,
                source_results=source_results,
            )
            self._finish_run(run.watch_id, result)
            return result
        except Exception as exc:
            return self._fail(
                run.id,
                token,
                "civic_worker_failed",
                f"{type(exc).__name__}: {exc}",
                source_results=source_results,
            )

    async def _gather_sources(
        self, run_id: int, watch_id: int, batch_id: int,
    ) -> tuple[CivicSourceExecution, ...]:
        async def one(entry: object) -> CivicSourceExecution:
            async with self._source_semaphore:
                now = self.now_fn()
                started_at = monotonic()
                self.event_logger.emit(
                    "civic.source.fetch.started",
                    watch_id=watch_id,
                    run_id=run_id,
                    batch_id=batch_id,
                    source_key=entry.source_key,
                    phase=CivicRunPhase.GATHERING,
                    status="started",
                )
                try:
                    result = self.source_executor(entry, now)
                    if inspect.isawaitable(result):
                        result = await result
                    if result.source_key != entry.source_key:
                        raise ValueError(
                            f"connector returned source_key {result.source_key!r}")
                    status = {
                        CivicSourceHealth.OK: CivicSourceExecutionStatus.OK,
                        CivicSourceHealth.EMPTY: CivicSourceExecutionStatus.EMPTY,
                        CivicSourceHealth.DEGRADED: CivicSourceExecutionStatus.DEGRADED,
                        CivicSourceHealth.TOMBSTONE: CivicSourceExecutionStatus.TOMBSTONE,
                    }[result.health]
                    self.event_logger.emit(
                        "civic.source.fetch.finished",
                        watch_id=watch_id,
                        run_id=run_id,
                        batch_id=batch_id,
                        source_key=entry.source_key,
                        phase=CivicRunPhase.GATHERING,
                        status=status,
                        reason=(
                            "degraded"
                            if status is CivicSourceExecutionStatus.DEGRADED
                            else None
                        ),
                        document_count=len(result.documents),
                        pages_fetched=result.pages_fetched,
                        elapsed_ms=(monotonic() - started_at) * 1000,
                    )
                    return CivicSourceExecution(
                        source_key=entry.source_key,
                        status=status,
                        documents=result.documents,
                        detail="; ".join(result.degraded_reasons),
                    )
                except Exception as exc:
                    outage = await self._site_outage_detail(entry, exc)
                    if outage is not None:
                        self.event_logger.emit(
                            "civic.source.fetch.finished",
                            watch_id=watch_id,
                            run_id=run_id,
                            batch_id=batch_id,
                            source_key=entry.source_key,
                            phase=CivicRunPhase.GATHERING,
                            status=CivicSourceExecutionStatus.UNAVAILABLE,
                            reason="site_unavailable",
                            error_class=type(exc).__name__,
                            elapsed_ms=(monotonic() - started_at) * 1000,
                        )
                        return CivicSourceExecution(
                            source_key=entry.source_key,
                            status=CivicSourceExecutionStatus.UNAVAILABLE,
                            detail=outage,
                        )
                    self.event_logger.emit(
                        "civic.source.fetch.failed",
                        watch_id=watch_id,
                        run_id=run_id,
                        batch_id=batch_id,
                        source_key=entry.source_key,
                        phase=CivicRunPhase.GATHERING,
                        status=CivicSourceExecutionStatus.FAILED,
                        error_code="source_fetch_failed",
                        error_class=type(exc).__name__,
                        elapsed_ms=(monotonic() - started_at) * 1000,
                    )
                    return CivicSourceExecution(
                        source_key=entry.source_key,
                        status=CivicSourceExecutionStatus.FAILED,
                        detail=f"{type(exc).__name__}: {exc}"[:MAX_ERROR_DETAIL_CHARS],
                    )

        return tuple(await asyncio.gather(*(one(entry) for entry in self.source_entries)))

    async def _site_outage_detail(self, entry: object, exc: Exception) -> str | None:
        """A detail when the listing and the verified health URL both got an outage answer."""
        listing = outage_status(exc)
        if (self.site_probe is None or listing is None
                or getattr(entry, "source_key", None) not in OUTAGE_HEALTH_URLS):
            return None
        try:
            health = await asyncio.to_thread(self.site_probe, entry)
        except Exception:  # noqa: BLE001 -- an unprobeable source keeps failing loudly
            return None
        if health not in OUTAGE_STATUSES:
            return None
        return f"site_unavailable: listing HTTP {listing}; health URL HTTP {health}"

    def _apply_outage_policy(
        self,
        results: tuple[CivicSourceExecution, ...],
        *,
        run_id: int,
        claim_token: str,
        watch_id: int,
        batch_id: int,
    ) -> tuple[CivicSourceExecution, ...]:
        """Hold isolated whole-site outages; anything our side may have caused still fails."""
        held = [result for result in results if result.held]
        succeeded = sum(result.succeeded for result in results)
        unconfirmed = bool(held) and (
            succeeded == 0 or len(held) > held_source_limit(len(results)))
        observations: dict[str, str | None] = {}
        for result in results:
            if result.succeeded:
                observations[result.source_key] = None
            elif result.held and not unconfirmed:
                observations[result.source_key] = result.detail
        episodes = observe_source_outages_if_claimed(
            self.conn,
            run_id=run_id,
            claim_token=claim_token,
            observations=observations,
            now=self.now_fn(),
            now_fn=self.now_fn,
        )
        if episodes is None:
            raise _CivicWorkerClaimLost
        final = []
        for result in results:
            if not result.held:
                final.append(result)
                continue
            if unconfirmed:
                final.append(replace(
                    result,
                    status=CivicSourceExecutionStatus.FAILED,
                    detail=f"site_unavailable_unconfirmed: {result.detail}"))
                continue
            episode = episodes[result.source_key]
            deadline = episode.unavailable_since + OUTAGE_ESCALATION_AFTER
            escalated = episode.last_unavailable_at >= deadline
            self.event_logger.emit(
                "civic.source.outage.held",
                watch_id=watch_id,
                run_id=run_id,
                batch_id=batch_id,
                source_key=result.source_key,
                phase=CivicRunPhase.GATHERING,
                status="escalated" if escalated else "held",
                deadline_at=deadline,
            )
            if escalated:
                final.append(replace(
                    result,
                    status=CivicSourceExecutionStatus.FAILED,
                    detail=(
                        f"site_unavailable_since "
                        f"{episode.unavailable_since.date().isoformat()}: {result.detail}")))
            else:
                final.append(result)
        return tuple(final)

    async def _fetch_source(self, entry: object, now: datetime) -> CivicFetchResult:
        if isinstance(entry, PartyMediaSourceEntry):
            return await asyncio.to_thread(
                fetch_entry,
                entry,
                now,
                transport=self._party_media_transport,
            )
        connector = entry.build()
        return await asyncio.to_thread(connector.fetch, now)

    async def _call_evidence_hydrator(
        self,
        document: CivicDocument,
        now: datetime,
    ) -> CivicHydrationResult:
        call = self.evidence_hydrator
        if (
            inspect.iscoroutinefunction(call)
            or inspect.iscoroutinefunction(getattr(call, "__call__", None))
        ):
            result = call(document, now)
            return await result if inspect.isawaitable(result) else result
        return await asyncio.to_thread(call, document, now)

    def _requires_evidence_hydration(self, document: CivicDocument) -> bool:
        profile = self.profiles.get(document.source_key)
        return bool(
            profile
            and profile.source_kind is not None
            and profile.source_kind.value == "party-primary"
            and not has_current_article_capture(document)
        )

    def _hold_corrupt_capture(
        self, candidate_id: int, run_id: int, claim_token: str,
        watch_id: int, batch_id: int,
    ) -> None:
        held = hold_capture_integrity_if_claimed(
            self.conn, candidate_id=candidate_id, run_id=run_id, claim_token=claim_token,
            now=self.now_fn(), now_fn=self.now_fn)
        if held is None:
            raise _CivicWorkerClaimLost
        self.event_logger.emit(
            "civic.evidence.integrity.failed",
            watch_id=watch_id, run_id=run_id, batch_id=batch_id,
            candidate_id=candidate_id, phase=self._active_phase.get(run_id),
            status="integrity_error", error_code="source_capture_integrity_error",
            reason=held.reason, attempt=held.ai_revision)

    def _hold_unready_source(
        self, candidate_id: int, run_id: int, claim_token: str,
        watch_id: int, batch_id: int, reason: str,
    ) -> None:
        plan = candidate_source_plan(self.conn, candidate_id)
        held = defer_evidence_refresh_if_claimed(
            self.conn, plan=plan, claim_token=claim_token, reason=reason,
            now=self.now_fn(), now_fn=self.now_fn)
        if held is None:
            raise _CivicWorkerClaimLost
        self.event_logger.emit(
            "civic.evidence.refresh.finished",
            watch_id=watch_id, run_id=run_id, batch_id=batch_id,
            candidate_id=candidate_id, phase=self._active_phase.get(run_id),
            status=held.status, reason=held.reason, attempt=held.ai_revision)

    async def _refresh_run_evidence(
        self, run_id: int, claim_token: str, watch_id: int, batch_id: int,
        *, candidate_ids: Sequence[int] | None = None,
    ) -> None:
        run = get_civic_run(self.conn, run_id)
        if run is None or run.deadline_at is None:
            raise ValueError("source refresh requires a claimed Run with a deadline")
        for candidate in list_run_candidates(self.conn, run_id):
            if candidate_ids is not None and candidate.id not in candidate_ids:
                continue
            self._require_claim(run_id, claim_token)
            if candidate.sealed_into_batch_at is not None or candidate.status not in {
                CivicCandidateStatus.DRAFTED, CivicCandidateStatus.RESEARCHING,
                CivicCandidateStatus.VERIFYING, CivicCandidateStatus.VERIFIED,
            }:
                continue
            try:
                plan = candidate_source_plan(self.conn, candidate.id)
            except CivicCaptureIntegrityError:
                self._hold_corrupt_capture(
                    candidate.id, run_id, claim_token, watch_id, batch_id)
                continue
            if plan.status in {EvidenceRefreshStatus.CURRENT, EvidenceRefreshStatus.PRESERVED}:
                continue
            reservation = None
            reason = plan.reason
            if plan.status is EvidenceRefreshStatus.PLANNED:
                try:
                    reservation = reserve_evidence_refresh_if_claimed(
                        self.conn, plan=plan, claim_token=claim_token,
                        now=self.now_fn(), now_fn=self.now_fn)
                except CivicHydrationBudgetError:
                    reason = "source_refresh_hydration_limit"
                except CivicSourceRefreshBlocked as exc:
                    reason = exc.reason
                else:
                    if reservation is None:
                        self._require_claim(run_id, claim_token)
                        reason = "source_refresh_reservation_pending"
                    elif reservation.status == "completed":
                        if candidate_source_plan(self.conn, candidate.id).status is not EvidenceRefreshStatus.CURRENT:
                            raise CivicCaptureIntegrityError(
                                "completed source refresh has no current evidence")
                        continue
                    else:
                        prepared = await self._await_with_lease(
                            prepare_evidence_refresh(
                                plan, hydrate=self._call_evidence_hydrator,
                                now_fn=self.now_fn, deadline_at=run.deadline_at),
                            run_id, claim_token)
                        if prepared.status is EvidenceRefreshStatus.PREPARED:
                            applied = apply_evidence_refresh_if_claimed(
                                self.conn, reservation=reservation, prepared=prepared,
                                now=self.now_fn(), now_fn=self.now_fn)
                            if applied is None:
                                raise _CivicWorkerClaimLost
                            self.event_logger.emit(
                                "civic.evidence.refresh.finished",
                                watch_id=watch_id, run_id=run_id, batch_id=batch_id,
                                candidate_id=candidate.id, phase=CivicRunPhase.GATHERING,
                                status=applied.status, reason=applied.reason,
                                document_count=len(prepared.captures), attempt=applied.ai_revision)
                            plan = candidate_source_plan(self.conn, candidate.id)
                            if plan.status in {
                                EvidenceRefreshStatus.CURRENT, EvidenceRefreshStatus.PRESERVED,
                            }:
                                continue
                            reservation = None
                            reason = plan.reason or "source_refresh_conflict"
                        else:
                            reason = prepared.reason or "source_refresh_incomplete"
            held = defer_evidence_refresh_if_claimed(
                self.conn, plan=plan, claim_token=claim_token,
                reservation=reservation, reason=reason or "source_refresh_required",
                now=self.now_fn(), now_fn=self.now_fn)
            if held is None:
                raise _CivicWorkerClaimLost
            self.event_logger.emit(
                "civic.evidence.refresh.finished",
                watch_id=watch_id, run_id=run_id, batch_id=batch_id,
                candidate_id=candidate.id, phase=CivicRunPhase.GATHERING,
                status=held.status, reason=held.reason, attempt=held.ai_revision)

    async def _prepare_candidate_documents(
        self,
        documents: Sequence[CivicDocument],
        *,
        watch_id: int,
        run_id: int,
        batch_id: int,
        claim_token: str,
    ) -> tuple[
        CivicClusteringResult,
        dict[str, PartySubjectResolution],
        int,
        tuple[CivicDocumentCluster, ...],
    ]:
        plan = cluster_civic_documents(
            documents,
            consumed_item_keys=list_consumed_item_keys(self.conn, watch_id),
        )
        watch = get_civic_watch(self.conn, watch_id)
        if watch is None:
            raise ValueError(f"no Civic Watch with id={watch_id}")
        subject_resolutions: dict[str, PartySubjectResolution] = {}
        replacements: dict[str, CivicDocument] = {}
        prepared_clusters: list[CivicDocumentCluster] = []
        hydration_deferred: list[CivicDocumentCluster] = []
        attempted = 0
        unavailable = 0
        replayed = 0
        for cluster in plan.clusters:
            targets = [
                document
                for document in cluster.documents
                if self._requires_evidence_hydration(document)
            ]
            if len(targets) > MAX_EVIDENCE_HYDRATIONS_PER_RUN:
                hydration_deferred.append(cluster)
                self.event_logger.emit(
                    "civic.evidence.hydration.deferred",
                    watch_id=watch_id, run_id=run_id, batch_id=batch_id,
                    phase=CivicRunPhase.GATHERING, status="deferred",
                    reason="source_refresh_hydration_limit")
                continue
            subject_resolutions[cluster.cluster_key] = resolve_cluster_party_subject(
                cluster, self.profiles)
            cluster_available = True
            reservation = None
            fetch_targets = targets
            if targets:
                work_key = "fresh:" + text_sha256(json.dumps([
                    (document.dedupe_key, document.canonical_url) for document in targets
                ], separators=(",", ":")))
                try:
                    reservation = reserve_hydration_if_claimed(
                        self.conn, run_id=run_id, claim_token=claim_token,
                        work_key=work_key, units=len(targets), documents=targets,
                        now=self.now_fn(), now_fn=self.now_fn)
                except CivicHydrationBudgetError:
                    hydration_deferred.append(cluster)
                    self.event_logger.emit(
                        "civic.evidence.hydration.deferred",
                        watch_id=watch_id, run_id=run_id, batch_id=batch_id,
                        phase=CivicRunPhase.GATHERING, status="deferred",
                        reason="source_refresh_hydration_limit")
                    continue
                if reservation is None:
                    self._require_claim(run_id, claim_token)
                    hydration_deferred.append(cluster)
                    self.event_logger.emit(
                        "civic.evidence.hydration.deferred",
                        watch_id=watch_id, run_id=run_id, batch_id=batch_id,
                        phase=CivicRunPhase.GATHERING, status="deferred",
                        reason="source_hydration_reservation_pending")
                    continue
                if reservation.status == "completed":
                    cached = hydration_documents(reservation)
                    replacements.update((document.dedupe_key, document) for document in cached)
                    replayed += len(cached)
                    fetch_targets = []
            failure_reason = None
            for document in fetch_targets:
                attempted += 1
                started_at = monotonic()
                result = await self._await_with_lease(
                    self._call_evidence_hydrator(document, self.now_fn()),
                    run_id,
                    claim_token,
                )
                if not isinstance(result, CivicHydrationResult):
                    raise TypeError("Civic evidence hydrator returned an invalid result")
                if result.document.dedupe_key != document.dedupe_key:
                    raise ValueError("Civic evidence hydration changed document identity")
                if result.status is CivicHydrationStatus.HYDRATED:
                    replacements[document.dedupe_key] = result.document
                else:
                    unavailable += 1
                    cluster_available = False
                    failure_reason = result.error_code or "source_hydration_not_performed"
                self.event_logger.emit(
                    "civic.evidence.hydration.finished",
                    watch_id=watch_id,
                    run_id=run_id,
                    batch_id=batch_id,
                    source_key=document.source_key,
                    phase=CivicRunPhase.GATHERING,
                    status=result.status,
                    error_code=result.error_code,
                    title_hydrated=bool(
                        result.document.raw_metadata.get("detail_title_hydrated")
                    ),
                    publication_date_hydrated=bool(
                        result.document.raw_metadata.get(
                            "detail_publication_date_hydrated"
                        )
                    ),
                    elapsed_ms=(monotonic() - started_at) * 1000,
                )
            if reservation is not None and reservation.status != "completed":
                if cluster_available:
                    completed = complete_hydration_if_claimed(
                        self.conn, reservation=reservation,
                        documents=tuple(replacements[document.dedupe_key] for document in targets),
                        now=self.now_fn(), now_fn=self.now_fn)
                    if completed is None:
                        raise _CivicWorkerClaimLost
                elif not fail_hydration_if_claimed(
                    self.conn, reservation=reservation,
                    reason=failure_reason or "source_hydration_unavailable",
                    now=self.now_fn(), now_fn=self.now_fn,
                ):
                    raise _CivicWorkerClaimLost
            if not cluster_available:
                hydration_deferred.append(cluster)
                continue
            prepared_cluster = CivicDocumentCluster(
                cluster_key=cluster.cluster_key,
                documents=tuple(
                    replacements.get(document.dedupe_key, document)
                    for document in cluster.documents
                ),
            )
            if event_date_for_cluster(prepared_cluster, watch) is None:
                hydration_deferred.append(cluster)
                self.event_logger.emit(
                    "civic.evidence.date.unavailable",
                    watch_id=watch_id,
                    run_id=run_id,
                    batch_id=batch_id,
                    phase=CivicRunPhase.GATHERING,
                    status="deferred",
                    error_code="source_publication_date_missing",
                )
                continue
            prepared_clusters.append(prepared_cluster)
        prepared = CivicClusteringResult(
            clusters=tuple(prepared_clusters),
            deferred_clusters=(
                *tuple(hydration_deferred),
                *plan.deferred_clusters,
            ),
            skipped_item_keys=plan.skipped_item_keys,
        )
        deferred = len(hydration_deferred)
        self.event_logger.emit(
            "civic.evidence.hydration.summary",
            watch_id=watch_id,
            run_id=run_id,
            batch_id=batch_id,
            phase=CivicRunPhase.GATHERING,
            status="degraded" if unavailable or deferred else "completed",
            document_count=attempted,
            succeeded_count=len(replacements),
            failed_count=unavailable,
            deferred_count=deferred,
            replayed_count=replayed,
        )
        return prepared, subject_resolutions, deferred, plan.deferred_clusters

    async def _call_researcher(
        self, claim: object, now: datetime,
    ) -> Sequence[CivicDocument]:
        result = self.researcher(claim, now)
        return await result if inspect.isawaitable(result) else result

    async def _await_with_lease(self, awaitable, run_id: int, claim_token: str):
        """Await external work while renewing, and settle its task before propagating claim loss."""
        task = asyncio.ensure_future(awaitable)
        try:
            while True:
                done, _pending = await asyncio.wait(
                    {task}, timeout=self.heartbeat_interval_seconds)
                if task in done:
                    result = await task
                    self._require_claim(run_id, claim_token)
                    return result
                self._require_claim(run_id, claim_token)
        except BaseException:
            if not task.done():
                task.cancel()
            try:
                await task
            except BaseException:
                pass
            raise

    def _start_phase(
        self,
        run_id: int,
        watch_id: int | None,
        phase: CivicRunPhase,
    ) -> None:
        self._phase_started_at[run_id] = monotonic()
        self._active_phase[run_id] = phase
        self.event_logger.emit(
            "civic.phase.started",
            watch_id=watch_id,
            run_id=run_id,
            batch_id=self._batch_ids.get(run_id),
            phase=phase,
            status="started",
        )

    def _finish_phase(
        self,
        run_id: int,
        watch_id: int | None,
        *,
        status: str,
    ) -> None:
        started_at = self._phase_started_at.pop(run_id, None)
        phase = self._active_phase.pop(run_id, None)
        if phase is None:
            return
        self.event_logger.emit(
            "civic.phase.finished",
            watch_id=watch_id,
            run_id=run_id,
            batch_id=self._batch_ids.get(run_id),
            phase=phase,
            status=status,
            elapsed_ms=(
                (monotonic() - started_at) * 1000
                if started_at is not None else None
            ),
        )

    def _fail_phase(
        self,
        run_id: int,
        watch_id: int | None,
        error_code: str,
        status: str,
    ) -> None:
        started_at = self._phase_started_at.pop(run_id, None)
        phase = self._active_phase.pop(run_id, None)
        if phase is None:
            return
        self.event_logger.emit(
            "civic.phase.failed",
            watch_id=watch_id,
            run_id=run_id,
            batch_id=self._batch_ids.get(run_id),
            phase=phase,
            status=status,
            error_code=error_code,
            elapsed_ms=(
                (monotonic() - started_at) * 1000
                if started_at is not None else None
            ),
        )

    def _finish_run(
        self,
        watch_id: int | None,
        result: CivicRunExecution,
    ) -> None:
        started_at = self._run_started_at.pop(result.run_id, None)
        self._batch_ids.pop(result.run_id, None)
        self.event_logger.emit(
            "civic.run.finished",
            watch_id=watch_id,
            run_id=result.run_id,
            batch_id=result.batch_id,
            status=result.status,
            candidate_count=result.candidate_count,
            completed_count=result.verified_count,
            source_failed_count=len(result.source_failures),
            error_code=result.error_code,
            elapsed_ms=(
                (monotonic() - started_at) * 1000
                if started_at is not None else None
            ),
        )

    def _emit_qualification(
        self,
        watch_id: int,
        run_id: int,
        batch_id: int,
        candidate_id: int,
        phase: CivicRunPhase,
        output,
        started_at: float,
    ) -> None:
        self.event_logger.emit(
            "civic.candidate.qualification.finished",
            watch_id=watch_id,
            run_id=run_id,
            batch_id=batch_id,
            candidate_id=candidate_id,
            phase=phase,
            status=output.qualification.outcome,
            reason=(
                output.qualification.reasons[0]
                if output.qualification.reasons else None
            ),
            replayed=output.replayed,
            elapsed_ms=(monotonic() - started_at) * 1000,
        )

    def _require_claim(self, run_id: int, token: str) -> None:
        if not heartbeat_civic_run(
                self.conn,
                run_id,
                token,
                self.now_fn(),
                self.lease_seconds,
                now_fn=self.now_fn):
            raise _CivicWorkerClaimLost
        run = get_civic_run(self.conn, run_id)
        if run is not None and run.cancel_requested:
            if not complete_civic_run(
                    self.conn,
                    run_id,
                    token,
                    CivicRunStatus.CANCELLED,
                    self.now_fn(),
                    now_fn=self.now_fn):
                raise _CivicWorkerClaimLost
            raise _CivicWorkerCancelled

    def _advance(self, run_id: int, token: str, phase: CivicRunPhase) -> None:
        self._require_claim(run_id, token)
        current = get_civic_run(self.conn, run_id)
        if not advance_civic_run_phase(
                self.conn, run_id, token, phase, now_fn=self.now_fn):
            raise _CivicWorkerClaimLost
        watch_id = current.watch_id if current is not None else None
        if current is not None and current.phase is not None:
            self._finish_phase(run_id, watch_id, status="completed")
        self._start_phase(run_id, watch_id, phase)

    def _complete(self, run_id: int, token: str) -> None:
        current = get_civic_run(self.conn, run_id)
        if not complete_civic_run(
                self.conn,
                run_id,
                token,
                CivicRunStatus.COMPLETED,
                self.now_fn(),
                now_fn=self.now_fn):
            raise _CivicWorkerClaimLost
        completed = get_civic_run(self.conn, run_id)
        if completed is not None and completed.status is CivicRunStatus.CANCELLED:
            raise _CivicWorkerCancelled
        self._finish_phase(
            run_id,
            current.watch_id if current is not None else None,
            status="completed",
        )

    def _fail(
        self,
        run_id: int,
        token: str,
        error_code: str,
        detail: str,
        *,
        source_results: tuple[CivicSourceExecution, ...] = (),
    ) -> CivicRunExecution:
        detail = detail[:MAX_ERROR_DETAIL_CHARS]
        current = get_civic_run(self.conn, run_id)
        completed = complete_civic_run(
            self.conn,
            run_id,
            token,
            CivicRunStatus.FAILED,
            self.now_fn(),
            now_fn=self.now_fn,
            error_code=error_code,
            error_detail=detail,
        )
        if not completed:
            self._fail_phase(run_id, current.watch_id if current else None, "claim_lost",
                             "stale_claim")
            result = CivicRunExecution(
                run_id=run_id,
                status=CivicRunExecutionStatus.STALE_CLAIM,
                source_results=source_results,
                error_code="claim_lost",
            )
            self._finish_run(current.watch_id if current else None, result)
            return result
        self._fail_phase(
            run_id,
            current.watch_id if current else None,
            error_code,
            "failed",
        )
        batch = get_batch_for_run(self.conn, run_id)
        result = CivicRunExecution(
            run_id=run_id,
            status=CivicRunExecutionStatus.FAILED,
            batch_id=batch.id if batch else None,
            source_results=source_results,
            error_code=error_code,
            error_detail=detail,
        )
        self._finish_run(current.watch_id if current else None, result)
        return result

    def _record_source_failures(
        self,
        run_id: int,
        watch_id: int,
        failures: Sequence[CivicSourceExecution],
    ) -> None:
        body = "\n".join(
            f"{failure.source_key}: {failure.status.value}: {failure.detail}"
            for failure in failures)
        title = (
            "source failures" if any(not failure.held for failure in failures)
            else "source outage holds")
        queue_notification(
            self.conn,
            watch_id,
            "civic_source_failures",
            f"civic-run:{run_id}:source-failures",
            f"Civic Run {run_id} {title}",
            self.now_fn(),
            run_id=run_id,
            body=body[:4000],
        )

    def _settle_subject_resolution(
        self,
        run_id: int,
        claim_token: str,
        candidate_id: int,
        resolution: PartySubjectResolution,
    ):
        reason = (
            CivicPolicyReason.SUBJECT_AMBIGUOUS_V1
            if resolution.status is PartySubjectResolutionStatus.AMBIGUOUS
            else CivicPolicyReason.SUBJECT_UNSUPPORTED_V1
        )
        output = settle_destination_subject_if_claimed(
            self.conn,
            run_id=run_id,
            claim_token=claim_token,
            candidate_id=candidate_id,
            reason=reason,
            now=self.now_fn(),
            now_fn=self.now_fn,
            engine_version=DESTINATION_V1_ENGINE_VERSION,
        )
        if output is None:
            raise _CivicWorkerClaimLost
        return output

__all__ = [
    "DEFAULT_DEADLINE_SECONDS",
    "DEFAULT_LEASE_SECONDS",
    "MAX_SOURCE_CONCURRENCY",
    "CivicCycleExecution",
    "CivicRunExecution",
    "CivicRunExecutionStatus",
    "CivicSourceExecution",
    "CivicSourceExecutionStatus",
    "CivicWorker",
]
