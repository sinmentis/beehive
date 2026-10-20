from __future__ import annotations

import io
import json
from datetime import timedelta
from functools import partial
from urllib.parse import urlsplit

import pytest

import beehive.civic.source_outages as source_outages
from beehive.civic.documents import CivicFetchResult, CivicSourceError, CivicSourceHealth
from beehive.civic.events import CivicEventLogger
from beehive.civic.fetching import CrawlPacer
from beehive.civic.scheduling import schedule_due_civic_runs
from beehive.civic.worker import (
    CivicRunExecutionStatus,
    CivicSourceExecutionStatus,
    CivicWorker,
)
from beehive.connectors.civic_catalog import (
    PARTY_MEDIA_USER_AGENT,
    PartyMediaSourceEntry,
    SourceAvailability,
)
from beehive.connectors.civic_sources import get_source
from beehive.connectors.http import ConnectorHttpError, ConnectorHttpErrorKind, HttpResponse
from beehive.db.civic_runs import claim_civic_run
from beehive.db.civic_source_outages import (
    get_source_outage,
    observe_source_outages_if_claimed,
)
from beehive.db.connection import connect, init_schema
from tests.civic.civic_worker_support import (
    MutableClock,
    SOURCE_ENTRY,
    T0,
    create_partymap_watch,
    housing_fetch_result,
    passing_green_gateway,
)

CONSERVATIVE = get_source("conservative")
VISION = get_source("vision-nz")
GREEN = get_source("green")
HELD = CivicSourceExecutionStatus.UNAVAILABLE
FAILED = CivicSourceExecutionStatus.FAILED
DAY = timedelta(days=1)
_KINDS = {
    401: ConnectorHttpErrorKind.ACCESS_DENIED,
    403: ConnectorHttpErrorKind.ACCESS_DENIED,
    404: ConnectorHttpErrorKind.NOT_FOUND,
    410: ConnectorHttpErrorKind.NOT_FOUND,
    400: ConnectorHttpErrorKind.PROTOCOL,
}


def _connector_error(status, kind=None):
    kind = kind or _KINDS.get(status, ConnectorHttpErrorKind.TRANSIENT)
    return ConnectorHttpError(kind, f"https://example.test/listing: HTTP {status}", status=status)


def _listing_failure(status=404, kind=None):
    def fail(entry):
        try:
            raise _connector_error(status, kind)
        except ConnectorHttpError as exc:
            raise CivicSourceError(entry.source_key, f"listing: {exc}") from exc

    return fail


def _database(tmp_path):
    conn = connect(str(tmp_path / "outages.db"))
    init_schema(conn)
    create_partymap_watch(conn)
    return conn, MutableClock()


def _seed(conn, source_key, *, since, last, observed):
    conn.execute(
        "INSERT INTO civic_source_outages (source_key, unavailable_since, last_unavailable_at, "
        "observed_at, detail) VALUES (?, ?, ?, ?, 'earlier')",
        (source_key, since.isoformat(), last.isoformat(), observed.isoformat()))
    conn.commit()


def _worker(conn, clock, *, down, health, entries=(SOURCE_ENTRY, CONSERVATIVE), stream=None,
            failure=None, before_fetch=None):
    probes = []
    failure = failure or _listing_failure()

    async def fetch(entry, now):
        if before_fetch is not None:
            before_fetch(entry)
        if entry.source_key in down:
            failure(entry)
        if entry.source_key == SOURCE_ENTRY.source_key:
            return housing_fetch_result()
        return CivicFetchResult(
            source_key=entry.source_key, documents=(), health=CivicSourceHealth.EMPTY,
            fetched_at=now)

    def probe(entry):
        probes.append(entry.source_key)
        if isinstance(health, BaseException):
            raise health
        return health

    worker = CivicWorker(
        conn,
        passing_green_gateway(),
        source_entries=entries,
        source_executor=fetch,
        site_probe=probe,
        now_fn=clock,
        event_logger=(
            CivicEventLogger(stream, now_fn=clock, correlation_id="civic-outage-test-01")
            if stream is not None else None),
    )
    return worker, probes


def _statuses(results):
    return sorted((result.source_key, result.status) for result in results)


async def test_whole_site_outage_is_held_and_reported_instead_of_failing(tmp_path):
    conn, clock = _database(tmp_path)
    stream = io.StringIO()
    worker, probes = _worker(conn, clock, down={"conservative"}, health=404, stream=stream)

    run = (await worker.run_due_cycle()).runs[0]

    assert run.status is CivicRunExecutionStatus.COMPLETED
    assert run.source_failures == ()
    assert _statuses(run.source_holds) == [("conservative", HELD)]
    assert run.source_holds[0].detail == (
        "site_unavailable: listing HTTP 404; health URL HTTP 404")
    assert probes == ["conservative"]
    outage = get_source_outage(conn, "conservative")
    assert (outage.unavailable_since, outage.last_unavailable_at) == (clock(), clock())
    held = [
        json.loads(line) for line in stream.getvalue().splitlines()
        if '"event":"civic.source.outage.held"' in line
    ]
    assert [(event["source_key"], event["status"], event["deadline_at"]) for event in held] == [
        ("conservative", "held", "2026-08-07T18:30:00.000Z")]
    [notice] = conn.execute(
        "SELECT subject, body FROM civic_notifications WHERE kind = 'civic_source_failures'"
    ).fetchall()
    assert notice["subject"].endswith("source outage holds")
    assert notice["body"] == (
        "conservative: unavailable: site_unavailable: listing HTTP 404; health URL HTTP 404")


@pytest.mark.parametrize(("listing", "health"), [
    (410, 404), (503, 502), (500, 404), (403, 403), (522, 521)])
async def test_server_errors_and_refusals_on_both_pages_are_outage_answers(
        tmp_path, listing, health):
    conn, clock = _database(tmp_path)
    worker, _ = _worker(
        conn, clock, down={"conservative"}, health=health, failure=_listing_failure(listing))

    run = (await worker.run_due_cycle()).runs[0]

    assert _statuses(run.source_holds) == [("conservative", HELD)]
    assert run.source_failures == ()


@pytest.mark.parametrize("health", [None, 429, RuntimeError("probe crashed")])
async def test_listing_failure_while_the_health_url_answers_still_fails(tmp_path, health):
    conn, clock = _database(tmp_path)
    worker, probes = _worker(conn, clock, down={"conservative"}, health=health)

    run = (await worker.run_due_cycle()).runs[0]

    assert _statuses(run.source_failures) == [("conservative", FAILED)]
    assert run.source_holds == ()
    assert probes == ["conservative"]
    assert get_source_outage(conn, "conservative") is None


def _raise(error):
    def fail(entry):
        try:
            raise error
        except ConnectorHttpError as exc:
            raise CivicSourceError(entry.source_key, f"listing: {exc}") from exc

    return fail


@pytest.mark.parametrize("failure", [
    _raise(ConnectorHttpError(ConnectorHttpErrorKind.TRANSIENT, "URLError: name resolution")),
    _raise(ConnectorHttpError(ConnectorHttpErrorKind.TRANSIENT, "TimeoutError: timed out")),
    _raise(ConnectorHttpError(ConnectorHttpErrorKind.TOO_LARGE, "body exceeds the cap")),
    _listing_failure(429),
    _listing_failure(400),
    _listing_failure(501, ConnectorHttpErrorKind.PROTOCOL),
    _listing_failure(511, ConnectorHttpErrorKind.PROTOCOL),
    _raise(ValueError("connector contract changed")),
], ids=["dns", "timeout", "too-large", "429", "400", "501", "511-captive-portal",
        "value-error"])
async def test_failures_without_an_outage_answer_fail_without_probing(tmp_path, failure):
    conn, clock = _database(tmp_path)
    worker, probes = _worker(
        conn, clock, down={"conservative"}, health=404, failure=failure)

    run = (await worker.run_due_cycle()).runs[0]

    assert _statuses(run.source_failures) == [("conservative", FAILED)]
    assert probes == []


@pytest.mark.parametrize("entry", [GREEN, SOURCE_ENTRY], ids=["homepage-refuses", "official"])
async def test_sources_without_a_verified_health_url_are_never_held(tmp_path, entry):
    conn, clock = _database(tmp_path)
    entries = (entry, CONSERVATIVE) if entry is SOURCE_ENTRY else (SOURCE_ENTRY, entry)
    worker, probes = _worker(
        conn, clock, down={entry.source_key}, health=404, entries=entries)

    run = (await worker.run_due_cycle()).runs[0]

    assert _statuses(run.source_failures) == [(entry.source_key, FAILED)]
    assert probes == []


@pytest.mark.parametrize(("age", "escalated"), [(7 * DAY - timedelta(seconds=1), False),
                                                (7 * DAY, True)])
async def test_outage_escalates_to_a_failure_after_seven_days(tmp_path, age, escalated):
    conn, clock = _database(tmp_path)
    started = clock() - age
    _seed(conn, "conservative", since=started, last=clock() - DAY, observed=clock() - DAY)
    worker, _ = _worker(conn, clock, down={"conservative"}, health=404)

    run = (await worker.run_due_cycle()).runs[0]

    if escalated:
        assert _statuses(run.source_failures) == [("conservative", FAILED)]
        assert run.source_failures[0].detail == (
            f"site_unavailable_since {started.date().isoformat()}: "
            "site_unavailable: listing HTTP 404; health URL HTTP 404")
    else:
        assert _statuses(run.source_holds) == [("conservative", HELD)]
        assert run.source_failures == ()
    outage = get_source_outage(conn, "conservative")
    assert outage.unavailable_since == started
    assert outage.last_unavailable_at == outage.observed_at == clock()


async def test_a_flapping_site_cannot_reset_its_escalation_clock(tmp_path):
    conn, clock = _database(tmp_path)
    _seed(conn, "conservative",
          since=clock() - 8 * DAY, last=clock() - 2 * DAY, observed=clock() - DAY)
    worker, _ = _worker(conn, clock, down={"conservative"}, health=404)

    run = (await worker.run_due_cycle()).runs[0]

    assert _statuses(run.source_failures) == [("conservative", FAILED)]
    assert get_source_outage(conn, "conservative").unavailable_since == clock() - 8 * DAY


@pytest.mark.parametrize(("since_last_outage", "kept"), [(3 * DAY, True), (7 * DAY, False)])
async def test_an_episode_ends_only_after_a_week_of_health(tmp_path, since_last_outage, kept):
    conn, clock = _database(tmp_path)
    last = clock() - since_last_outage
    _seed(conn, "conservative", since=last - 2 * DAY, last=last, observed=clock() - DAY)
    worker, probes = _worker(conn, clock, down=frozenset(), health=404)

    run = (await worker.run_due_cycle()).runs[0]

    assert run.source_failures == run.source_holds == ()
    assert probes == []
    outage = get_source_outage(conn, "conservative")
    if kept:
        assert (outage.last_unavailable_at, outage.observed_at) == (last, clock())
    else:
        assert outage is None



async def _daily_statuses(conn, clock, down_by_day):
    statuses = []
    for day, down in enumerate(down_by_day):
        clock.now = T0 + day * DAY
        worker, _ = _worker(conn, clock, down=down, health=404)
        run = (await worker.run_due_cycle()).runs[0]
        [conservative] = [
            result for result in run.source_results if result.source_key == "conservative"]
        statuses.append(conservative.status)
    return statuses


async def test_daily_runs_escalate_a_site_that_flaps_back_briefly(tmp_path):
    conn, clock = _database(tmp_path)
    outage, healthy = {"conservative"}, frozenset()

    statuses = await _daily_statuses(conn, clock, [outage] * 6 + [healthy] + [outage])

    assert statuses == [HELD] * 6 + [CivicSourceExecutionStatus.EMPTY] + [FAILED]
    assert get_source_outage(conn, "conservative").unavailable_since == T0


async def test_daily_runs_start_a_fresh_episode_after_a_healthy_week(tmp_path):
    conn, clock = _database(tmp_path)
    outage, healthy = {"conservative"}, frozenset()

    statuses = await _daily_statuses(conn, clock, [outage] * 2 + [healthy] * 7 + [outage])

    assert statuses == [HELD] * 2 + [CivicSourceExecutionStatus.EMPTY] * 7 + [HELD]
    assert get_source_outage(conn, "conservative").unavailable_since == T0 + 9 * DAY

async def test_a_stale_episode_restarts_instead_of_escalating(tmp_path):
    conn, clock = _database(tmp_path)
    _seed(conn, "conservative",
          since=clock() - 20 * DAY, last=clock() - 10 * DAY, observed=clock() - 4 * DAY)
    worker, _ = _worker(conn, clock, down={"conservative"}, health=404)

    run = (await worker.run_due_cycle()).runs[0]

    assert _statuses(run.source_holds) == [("conservative", HELD)]
    assert get_source_outage(conn, "conservative").unavailable_since == clock()


async def test_every_source_down_is_treated_as_our_failure(tmp_path):
    conn, clock = _database(tmp_path)
    earlier = clock() - DAY
    _seed(conn, "conservative", since=earlier, last=earlier, observed=earlier)
    worker, _ = _worker(
        conn, clock, down={"conservative", SOURCE_ENTRY.source_key}, health=404)

    run = (await worker.run_due_cycle()).runs[0]

    assert run.status is CivicRunExecutionStatus.FAILED
    assert run.error_code == "all_civic_sources_failed"
    assert {result.status for result in run.source_results} == {FAILED}
    [conservative] = [
        result for result in run.source_results if result.source_key == "conservative"]
    assert conservative.detail.startswith("site_unavailable_unconfirmed: ")
    assert get_source_outage(conn, "conservative").observed_at == earlier


async def test_too_many_simultaneous_outages_are_not_held(tmp_path):
    conn, clock = _database(tmp_path)
    worker, _ = _worker(
        conn, clock, down={"conservative", "vision-nz"}, health=404,
        entries=(SOURCE_ENTRY, CONSERVATIVE, VISION))

    run = (await worker.run_due_cycle()).runs[0]

    assert _statuses(run.source_failures) == [("conservative", FAILED), ("vision-nz", FAILED)]
    assert run.source_holds == ()
    assert get_source_outage(conn, "conservative") is None
    assert get_source_outage(conn, "vision-nz") is None


async def test_a_run_that_lost_its_claim_writes_no_outage(tmp_path):
    conn, clock = _database(tmp_path)

    def steal_claim(entry):
        if entry.source_key == SOURCE_ENTRY.source_key:
            conn.execute(
                "UPDATE civic_runs SET claim_token = 'claim-taken-by-another-worker' "
                "WHERE status = 'processing'")
            conn.commit()

    worker, _ = _worker(
        conn, clock, down={"conservative"}, health=404, before_fetch=steal_claim)

    run = (await worker.run_due_cycle()).runs[0]

    assert run.status is CivicRunExecutionStatus.STALE_CLAIM
    assert get_source_outage(conn, "conservative") is None


def test_outage_observations_are_written_only_under_a_live_claim(tmp_path):
    conn, _ = _database(tmp_path)
    run = schedule_due_civic_runs(conn, T0)[0].run
    claimed = claim_civic_run(conn, run.id, T0, 300, 1200).run
    observe = partial(
        observe_source_outages_if_claimed, conn, run_id=run.id,
        observations={"conservative": "site_unavailable"})

    assert observe(claim_token="not-this-runs-claim", now=T0) is None
    assert observe(claim_token=claimed.claim_token, now=T0 + timedelta(seconds=300)) is None
    assert get_source_outage(conn, "conservative") is None
    episodes = observe(claim_token=claimed.claim_token, now=T0)
    assert episodes["conservative"].unavailable_since == T0


def test_only_the_production_fetch_path_probes_real_sites(tmp_path):
    conn, _ = _database(tmp_path)

    async def fetch(entry, now):
        raise AssertionError("not called")

    injected = CivicWorker(
        conn, passing_green_gateway(), source_entries=(CONSERVATIVE,), source_executor=fetch)
    default = CivicWorker(conn, passing_green_gateway(), source_entries=(CONSERVATIVE,))

    assert injected.site_probe is None
    assert default.site_probe.func is source_outages.probe_health_url
    assert default.site_probe.keywords["transport"].pacer is default._party_media_transport.pacer


def test_health_probe_is_one_paced_request_with_the_party_user_agent(monkeypatch):
    calls = []

    def fetch_bytes(url, **kwargs):
        calls.append((url, kwargs))
        return HttpResponse(body=b"<html></html>", content_type="text/html", final_url=url)

    monkeypatch.setattr(source_outages, "fetch_bytes", fetch_bytes)
    ticks = iter([0.0, 1.0, 10.0])
    slept = []
    pacer = CrawlPacer(clock=lambda: next(ticks), sleeper=slept.append)
    pacer.wait_for("https://www.conservatives.nz/sitemap.xml", 10.0)

    transport = source_outages.health_probe_transport(pacer)

    assert source_outages.probe_health_url(CONSERVATIVE, transport) is None
    [(url, kwargs)] = calls
    assert url == "https://www.conservatives.nz/"
    assert kwargs["max_attempts"] == 1
    assert kwargs["user_agent"] == PARTY_MEDIA_USER_AGENT
    assert kwargs["allowed_hosts"] == frozenset(CONSERVATIVE.hosts)
    assert slept == [9.0]


@pytest.mark.parametrize(("raised", "expected"), [
    (_connector_error(404), 404),
    (_connector_error(503), 503),
    (_connector_error(403), 403),
    (_connector_error(429), None),
    (_connector_error(511, ConnectorHttpErrorKind.PROTOCOL), None),
    (ConnectorHttpError(ConnectorHttpErrorKind.TRANSIENT, "URLError: name resolution"), None),
    (ConnectorHttpError(ConnectorHttpErrorKind.TOO_LARGE, "body exceeds the cap"), None),
])
def test_health_probe_reports_only_outage_answers(monkeypatch, raised, expected):
    def fetch_bytes(url, **kwargs):
        raise raised

    monkeypatch.setattr(source_outages, "fetch_bytes", fetch_bytes)
    transport = source_outages.health_probe_transport(CrawlPacer(sleeper=lambda _seconds: None))

    assert source_outages.probe_health_url(CONSERVATIVE, transport) == expected


def test_sources_without_a_verified_health_url_are_not_requested(monkeypatch):
    def fetch_bytes(url, **kwargs):
        raise AssertionError(f"requested {url}")

    monkeypatch.setattr(source_outages, "fetch_bytes", fetch_bytes)
    transport = source_outages.health_probe_transport(CrawlPacer(sleeper=lambda _seconds: None))

    assert source_outages.probe_health_url(GREEN, transport) is None


def test_verified_health_urls_are_each_sources_own_homepage():
    for source_key, url in source_outages.OUTAGE_HEALTH_URLS.items():
        entry = get_source(source_key)
        parts = urlsplit(url)
        assert isinstance(entry, PartyMediaSourceEntry), source_key
        assert entry.availability is SourceAvailability.ENABLED, source_key
        assert (parts.scheme, parts.path, parts.query) == ("https", "/", ""), source_key
        assert parts.hostname in entry.hosts, source_key
    refuses_page_requests = {"green", "nz-first", "te-pati-maori", "opportunity"}
    assert not refuses_page_requests & set(source_outages.OUTAGE_HEALTH_URLS)
