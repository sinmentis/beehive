from __future__ import annotations

import asyncio
import hashlib
import io
import json

from beehive.civic.events import CivicEventLogger
from beehive.civic.worker import CivicRunExecutionStatus, CivicWorker
from beehive.db.connection import connect, init_schema
from tests.civic.civic_worker_support import (
    SOURCE_ENTRY,
    T0,
    create_partymap_watch,
    housing_fetch_result,
    passing_green_gateway,
)

CORRELATION_ID = "civic-test-run-0001"


class FlushTrackingStream(io.StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.flush_count = 0

    def flush(self) -> None:
        self.flush_count += 1
        super().flush()


def _events(stream: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_event_logger_writes_canonical_ndjson_and_flushes_each_event():
    stream = FlushTrackingStream()
    logger = CivicEventLogger(stream, now_fn=lambda: T0, correlation_id=CORRELATION_ID)

    logger.emit(
        "civic.test.finished",
        run_id=7,
        watch_id=3,
        status="completed",
        title_hydrated=True,
        publication_date_hydrated=False,
        correlation_id="attempted-override",
    )

    assert stream.flush_count == 1
    assert stream.getvalue().endswith("\n")
    event = _events(stream)[0]
    assert event == {
        "correlation_id": CORRELATION_ID,
        "event": "civic.test.finished",
        "publication_date_hydrated": False,
        "run_id": 7,
        "schema_version": "partymap.civic.event.v1",
        "status": "completed",
        "timestamp": "2026-07-31T18:30:00.000Z",
        "title_hydrated": True,
        "watch_id": 3,
    }
    assert stream.getvalue().strip() == json.dumps(
        event, ensure_ascii=True, allow_nan=False, separators=(",", ":"), sort_keys=True
    )


def test_event_logger_redacts_secrets_and_drops_content_fields():
    stream = FlushTrackingStream()
    logger = CivicEventLogger(stream, now_fn=lambda: T0, correlation_id=CORRELATION_ID)
    sensitive = (
        "github_pat_deadbeef ghp_deadbeef Bearer bearer-secret "
        "CLOUDFLARE_API_TOKEN=cf-secret CF_API_TOKEN cf-secret-two "
        "Authorization: header-secret Cookie=session-secret "
        "-----BEGIN PRIVATE KEY----- private-material -----END PRIVATE KEY----- "
        "prompt=private-prompt model_response=private-output"
    )

    logger.emit(
        "civic.test.failed",
        status="failed",
        error_code="fixture_failed",
        detail=sensitive,
        evidence_text="private-evidence",
        prompt="private-prompt-field",
        model_response="private-model-output-field",
        title="private-title",
        url="https://private.example/item",
    )

    rendered = stream.getvalue()
    assert "[REDACTED]" in rendered
    for forbidden in (
        "github_pat_",
        "ghp_",
        "Bearer",
        "bearer-secret",
        "CLOUDFLARE_API_TOKEN",
        "CF_API_TOKEN",
        "private-material",
        "PRIVATE KEY",
        "private-evidence",
        "private-prompt",
        "private-output",
        "private-title",
        "private.example",
    ):
        assert forbidden not in rendered


def test_invalid_or_secret_correlation_id_is_hashed_without_leaking():
    stream = FlushTrackingStream()
    unsafe = "github_pat_private-correlation-value"
    logger = CivicEventLogger(
        stream,
        now_fn=lambda: T0,
        correlation_id=unsafe,
    )

    logger.emit("civic.test.started", status="started")

    event = _events(stream)[0]
    assert event["correlation_id"] == hashlib.sha256(unsafe.encode()).hexdigest()
    assert unsafe not in stream.getvalue()


def test_operational_identifiers_allow_token_urlsafe_prefixes_and_private_classes():
    stream = FlushTrackingStream()
    logger = CivicEventLogger(stream, now_fn=lambda: T0, correlation_id=CORRELATION_ID)

    logger.emit(
        "civic.test.failed",
        status="failed",
        idempotency_key="_leading-underscore-token",
        source_key="-leading-hyphen-token",
        error_class="_CivicWorkerClaimLost",
    )

    event = _events(stream)[0]
    assert event["idempotency_key"] == "_leading-underscore-token"
    assert event["source_key"] == "-leading-hyphen-token"
    assert event["error_class"] == "_CivicWorkerClaimLost"


def test_operational_identifiers_still_reject_unsafe_delimiters():
    stream = FlushTrackingStream()
    logger = CivicEventLogger(stream, now_fn=lambda: T0, correlation_id=CORRELATION_ID)

    for value in ("contains space", 'contains"quote', "contains=equals"):
        logger.emit("civic.test.failed", status="failed", error_class=value)

    assert [event["error_class"] for event in _events(stream)] == [
        "[redacted]",
        "[redacted]",
        "[redacted]",
    ]


def test_logger_generates_one_distinct_safe_id_per_instance():
    first_stream = FlushTrackingStream()
    second_stream = FlushTrackingStream()
    first = CivicEventLogger(first_stream, now_fn=lambda: T0)
    second = CivicEventLogger(second_stream, now_fn=lambda: T0)

    first.emit("civic.test.started", status="started")
    first.emit("civic.test.finished", status="completed")
    second.emit("civic.test.started", status="started")

    first_ids = {event["correlation_id"] for event in _events(first_stream)}
    second_id = _events(second_stream)[0]["correlation_id"]
    assert len(first_ids) == 1
    assert next(iter(first_ids)) != second_id
    assert all(len(correlation_id) == 32 for correlation_id in first_ids | {second_id})


async def test_worker_events_correlate_and_bracket_material_phases(tmp_path):
    conn = connect(str(tmp_path / "events.db"))
    init_schema(conn)
    watch = create_partymap_watch(conn)
    stream = FlushTrackingStream()
    logger = CivicEventLogger(stream, now_fn=lambda: T0, correlation_id=CORRELATION_ID)

    async def fetch(_entry, _now):
        return housing_fetch_result()

    worker = CivicWorker(
        conn,
        passing_green_gateway(),
        source_entries=(SOURCE_ENTRY,),
        source_executor=fetch,
        now_fn=lambda: T0,
        event_logger=logger,
    )
    execution = (await worker.run_due_cycle()).runs[0]
    events = _events(stream)

    assert execution.status is CivicRunExecutionStatus.COMPLETED
    assert stream.flush_count == len(events)
    assert {event["correlation_id"] for event in events} == {CORRELATION_ID}
    run_events = [event for event in events if event.get("run_id") == execution.run_id]
    assert run_events
    assert all(event.get("watch_id") == watch.id for event in run_events)
    candidate_events = [
        event for event in run_events if event["event"].startswith("civic.candidate.")
    ]
    assert candidate_events
    assert all(
        event.get("batch_id") == execution.batch_id for event in candidate_events
    )
    assert all(event.get("candidate_id") for event in candidate_events)

    names = [event["event"] for event in events]
    assert names.index("civic.source.fetch.started") < names.index(
        "civic.source.fetch.finished"
    )
    assert names.index("civic.candidate.ai.started") < names.index(
        "civic.candidate.ai.finished"
    )
    assert names.index("civic.ai.stage.started") < names.index(
        "civic.ai.stage.finished"
    )
    assert names.index("civic.batch.seal.started") < names.index(
        "civic.batch.seal.finished"
    )
    phase_events = [
        event for event in run_events if event["event"].startswith("civic.phase.")
    ]
    for phase in ("gathering", "screening", "researching", "verifying", "publishing"):
        starts = [
            index
            for index, event in enumerate(phase_events)
            if event["event"] == "civic.phase.started" and event["phase"] == phase
        ]
        finishes = [
            index
            for index, event in enumerate(phase_events)
            if event["event"] == "civic.phase.finished" and event["phase"] == phase
        ]
        assert starts and finishes and starts[0] < finishes[-1]

    rendered = stream.getvalue()
    document = housing_fetch_result().documents[0]
    assert document.title not in rendered
    assert document.excerpt not in rendered
    assert document.canonical_url not in rendered


async def test_source_start_event_is_flushed_before_fetch_returns(tmp_path):
    conn = connect(str(tmp_path / "blocking.db"))
    init_schema(conn)
    create_partymap_watch(conn)
    stream = FlushTrackingStream()
    logger = CivicEventLogger(stream, now_fn=lambda: T0, correlation_id=CORRELATION_ID)
    fetch_entered = asyncio.Event()
    release_fetch = asyncio.Event()

    async def fetch(_entry, _now):
        fetch_entered.set()
        await release_fetch.wait()
        return housing_fetch_result()

    worker = CivicWorker(
        conn,
        passing_green_gateway(),
        source_entries=(SOURCE_ENTRY,),
        source_executor=fetch,
        now_fn=lambda: T0,
        event_logger=logger,
    )
    task = asyncio.create_task(worker.run_due_cycle())
    await fetch_entered.wait()

    assert not task.done()
    assert _events(stream)[-1]["event"] == "civic.source.fetch.started"
    assert _events(stream)[-1]["correlation_id"] == CORRELATION_ID
    assert stream.flush_count == len(_events(stream))

    release_fetch.set()
    await task
