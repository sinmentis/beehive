"""The Owner's workspace: research and the watch list share the admin datasheet shell with their
own two-chapter rail, the watch list puts failed reminders first, and each research page shows
one part of a session."""
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from beehive.auth.tokens import sign_session_id
from beehive.connectors.base import RawItem
from beehive.db.channels import create_channel
from beehive.db.connection import connect, init_schema
from beehive.db.evidence_items import upsert_evidence_item
from beehive.db.evidence_state import create_evidence_state_revision
from beehive.db.items import insert_new
from beehive.db.evidence_state import get_latest_evidence_state_revision
from beehive.db.research_plan_revisions import create_plan_revision
from beehive.db.research_runs import claim_research_run, complete_research_run, enqueue_research_run
from beehive.db.research_sessions import create_research_session
from beehive.db.research_snapshots import add_snapshot_items, create_snapshot, seal_snapshot
from beehive.db.research_sources import create_research_source
from beehive.db.research_syntheses import create_synthesis
from beehive.db.sessions import create_session
from beehive.db.sources import create_source
from beehive.db.tracker_watches import add_tracker_watch, count_failed_tracker_reminders
from beehive.domain.research import (ClaimProvenance, EvidenceCitation, EvidenceQuality,
                                      ResearchRunStatus, ResearchSourceOrigin, SufficiencyState,
                                      SynthesisClaim, SynthesisSection)
from beehive.channels.views import WatchlistQuery, build_watchlist_page
from beehive.localization import localizer_for
from beehive.web.app import create_app
from beehive.web.deps import SESSION_COOKIE_NAME
from scripts.set_admin_password import set_admin_password

T0 = datetime(2026, 7, 15, 0, 0, 0, tzinfo=timezone.utc)
_SECRET = "test-secret-at-least-32-characters-long"


@pytest.fixture
def conn(tmp_path):
    path = str(tmp_path / "test.db")
    c = connect(path)
    init_schema(c)
    return path, c


@pytest.fixture
def authed_client(conn, monkeypatch):
    path, c = conn
    monkeypatch.setenv("DIGEST_EMAIL_TO", "owner@example.com")
    set_admin_password(path, "correct-password")
    create_session(c, "sess1", "csrf1", "2099-01-01T00:00:00")
    client = TestClient(create_app(path, session_secret=_SECRET), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, sign_session_id("sess1", _SECRET))
    return client


def _lot(c, external_id: str, *, closes_in_hours: float = 4) -> int:
    """A watched auction lot in a Tracker Channel."""
    channel = c.execute("SELECT id FROM channels WHERE name = 'Auctions'").fetchone()
    channel_id = channel["id"] if channel else create_channel(
        c, "Auctions", "interesting auction lots", kind="tracker")
    source = c.execute(
        "SELECT id FROM sources WHERE channel_id = ? AND type = 'all_about_auctions'",
        (channel_id,),
    ).fetchone()
    source_id = source["id"] if source else create_source(c, channel_id, "all_about_auctions", {})
    closes_at = datetime.now(timezone.utc) + timedelta(hours=closes_in_hours)
    insert_new(c, source_id, RawItem(
        external_id=external_id,
        title=f"Lot {external_id}",
        url=f"https://auctions.allaboutauctions.co.nz/lot/{external_id}",
        raw_metadata={
            "auction_title": "Weekly auction",
            "closing_at": closes_at.isoformat(),
            "currency_code": "NZD",
            "current_bid": 50.0,
        },
    ))
    item_id = c.execute(
        "SELECT id FROM items WHERE external_id = ?", (external_id,)).fetchone()[0]
    add_tracker_watch(c, item_id, datetime.now(timezone.utc))
    return item_id


def _close(c, item_id: int) -> None:
    """A lot cannot be watched once closed, so tests watch it first and then close it."""
    row = c.execute("SELECT raw_metadata FROM items WHERE id = ?", (item_id,)).fetchone()
    metadata = json.loads(row["raw_metadata"])
    metadata["closing_at"] = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    c.execute("UPDATE items SET raw_metadata = ? WHERE id = ?", (json.dumps(metadata), item_id))
    c.commit()


def _fail_reminder(c, item_id: int, error: str = "ACS 401 Unauthorized") -> None:
    c.execute("UPDATE auction_watches SET last_error = ? WHERE item_id = ?", (error, item_id))
    c.commit()


def _run(c, session_id: int, status: ResearchRunStatus, *, collect: bool | None = None,
         **error) -> int:
    run = enqueue_research_run(c, session_id, T0)
    lease = claim_research_run(c, run.id, T0, lease_seconds=60, deadline_seconds=1200)
    collect = status is ResearchRunStatus.COMPLETED if collect is None else collect
    if collect:
        item = upsert_evidence_item(
            c, session_id, _session_source(c, session_id), f"e{run.id}", "Rates held",
            "https://example.com/article", EvidenceQuality.PRIMARY, T0, snippet="A snippet.")
        snapshot_id = create_snapshot(c, session_id, run.id, T0).id
        add_snapshot_items(c, snapshot_id, [item.id], T0)
        seal_snapshot(c, snapshot_id, T0)
        revision = create_evidence_state_revision(c, session_id, snapshot_id, [item.id], T0)
    if collect and status is ResearchRunStatus.COMPLETED:
        create_synthesis(c, session_id, revision.id, SufficiencyState.PARTIAL, (
            SynthesisClaim(
                text="Rates held steady this quarter", section=SynthesisSection.BOTTOM_LINE,
                provenance=ClaimProvenance.EVIDENCE,
                citations=(EvidenceCitation(
                    evidence_item_id=item.id, citation_number=item.citation_number),)),
        ), "gpt-5", "en", T0)
    # Finished after the session was created, so it counts as a result the Owner has not seen.
    complete_research_run(
        c, run.id, lease.run.claim_token, status, T0 + timedelta(seconds=30), **error)
    return run.id


def _session_source(c, session_id: int) -> int:
    row = c.execute(
        "SELECT id FROM research_sources WHERE session_id = ? ORDER BY id LIMIT 1",
        (session_id,),
    ).fetchone()
    return row["id"]


def _session(c, question: str = "What is happening with rates?") -> int:
    session_id = create_research_session(c, question, T0).id
    create_research_source(c, session_id, "rbnz_news", {}, ResearchSourceOrigin.OWNER, T0)
    return session_id


def _cited_item_id(c, session_id: int) -> int:
    return c.execute(
        "SELECT id FROM research_evidence_items WHERE session_id = ? ORDER BY id DESC LIMIT 1",
        (session_id,),
    ).fetchone()["id"]


# ============================================================================
# Shell
# ============================================================================

@pytest.mark.parametrize("path", ["/research", "/research/new", "/watchlist"])
def test_workspace_pages_use_the_workspace_rail_in_the_admin_datasheet(conn, authed_client, path):
    page = authed_client.get(path)
    assert page.status_code == 200
    assert "Workspace direction contract" in page.text
    assert "/static/admin.css" in page.text and "/static/beehive.css" not in page.text
    rail = page.text[page.text.index('<ol class="toc">'):page.text.index("</nav>")]
    assert 'href="/research"' in rail and 'href="/watchlist"' in rail
    assert "/admin/?tab=" not in rail
    foot = page.text[page.text.index('<div class="adm-toc-foot">'):]
    assert 'href="/admin/"' in foot and 'action="/admin/logout"' in foot


def test_rail_counts_failed_reminders_in_amber_and_unread_results_in_blue(conn, authed_client):
    _, c = conn
    _fail_reminder(c, _lot(c, "failing"))
    _lot(c, "fine")
    _run(c, _session(c), ResearchRunStatus.COMPLETED)
    assert count_failed_tracker_reminders(c) == 1

    page = authed_client.get("/watchlist").text
    watch_link = re.search(r'<a href="/watchlist"[^>]*>.*?</a>', page, re.S).group(0)
    assert 'class="toc-count"' in watch_link and ">1<" in watch_link
    research_link = re.search(r'<a href="/research"[^>]*>.*?</a>', page, re.S).group(0)
    assert 'class="toc-count is-info"' in research_link


def test_session_subpage_is_current_in_the_rail_under_its_chapter(conn, authed_client):
    _, c = conn
    session_id = _session(c)
    page = authed_client.get(f"/research/{session_id}?tab=evidence").text
    assert f'<a href="/research/{session_id}?tab=evidence" aria-current="page">' in page
    assert '<a href="/research" aria-current="true">' in page


# ============================================================================
# Watch list
# ============================================================================

def test_watchlist_lists_every_failed_reminder_first_whatever_the_filter(conn, authed_client):
    _, c = conn
    failing = _lot(c, "failing")
    _fail_reminder(c, failing, "ACS 401 Unauthorized: connection string expired")
    _lot(c, "fine")

    # The closed filter hides both active lots, but the failure is still listed first.
    page = authed_client.get("/watchlist?status=closed").text
    attention = page[page.index('id="attention"'):page.index('id="lots"')]
    assert "The reminder for “Lot failing” was not sent" in attention
    assert f'action="/items/{failing}/retry-reminder"' in attention
    assert "connection string expired" in attention
    assert f"watchlist-item-{failing}" not in page


def test_watchlist_says_so_when_nothing_needs_attention(conn, authed_client, monkeypatch):
    _, c = conn
    _lot(c, "fine")
    page = authed_client.get("/watchlist").text
    assert "Every reminder was sent or is scheduled." in page

    monkeypatch.delenv("DIGEST_EMAIL_TO")
    page = authed_client.get("/watchlist").text
    assert "No reminder email is configured." in page
    assert "Every reminder was sent or is scheduled." not in page


def test_stopping_a_watch_refreshes_the_list_counts_and_warnings_together(conn, authed_client):
    _, c = conn
    failing = _lot(c, "failing")
    _fail_reminder(c, failing)
    _lot(c, "fine")
    page = authed_client.get("/watchlist?status=all&q=Lot").text
    row = page[page.index(f'id="watchlist-item-{failing}"'):]
    row = row[:row.index("</tr>")]
    assert f'<form method="post" action="/items/{failing}/watch"' in row
    assert 'name="origin" value="watchlist"' in row
    assert 'name="next_url" value="/watchlist?status=all&amp;q=Lot"' in row
    # The whole list region, the head counts and the rail count come from the fresh page.
    assert 'hx-target="#watch-body" hx-select="#watch-body" hx-swap="outerHTML"' in row
    assert 'hx-select-oob="#watch-meta,#toc-count-watchlist"' in row
    assert 'form="watchlist-bulk-form"' in row

    for headers in ({"HX-Request": "true"}, {}):
        _fail_reminder(c, failing)
        c.execute("DELETE FROM auction_watches WHERE item_id = ?", (failing,))
        c.commit()
        add_tracker_watch(c, failing, datetime.now(timezone.utc))
        _fail_reminder(c, failing)
        resp = authed_client.post(f"/items/{failing}/watch", headers=headers, data={
            "csrf_token": "csrf1", "origin": "watchlist",
            "next_url": "/watchlist?status=all&q=Lot"})
        assert resp.status_code == 303
        assert resp.headers["location"] == "/watchlist?status=all&q=Lot"

    fresh = authed_client.get("/watchlist?status=all&q=Lot").text
    assert f"watchlist-item-{failing}" not in fresh
    assert "was not sent" not in fresh
    assert 'id="toc-count-watchlist"></span>' in fresh
    assert 'id="watch-meta">' in fresh


def test_stopping_a_watch_never_redirects_off_site(conn, authed_client):
    _, c = conn
    item_id = _lot(c, "lot")
    resp = authed_client.post(f"/items/{item_id}/watch", data={
        "csrf_token": "csrf1", "origin": "watchlist", "next_url": "https://example.com/"})
    assert resp.headers["location"] == "/watchlist"


def test_watchlist_ignores_an_unknown_retry_result(conn, authed_client):
    assert authed_client.get("/watchlist?retry=not-a-result").status_code == 200


def test_clearing_closed_watches_asks_once_without_typing(conn, authed_client):
    _, c = conn
    _close(c, _lot(c, "closed"))
    page = authed_client.get("/watchlist?status=all").text
    danger = page[page.index('id="clear-closed"'):]
    danger = danger[:danger.index("</details>")]
    assert 'action="/watchlist/clear-closed"' in danger
    assert 'name="confirmation"' not in danger
    assert "This cannot be undone." in danger


# ============================================================================
# Research list and session pages
# ============================================================================

def test_research_list_shows_runs_evidence_and_conclusion_per_session(conn, authed_client):
    _, c = conn
    session_id = _session(c)
    _run(c, session_id, ResearchRunStatus.FAILED, error_code="synthesis_failed")
    last = _run(c, session_id, ResearchRunStatus.COMPLETED)
    page = authed_client.get("/research").text
    row = page[page.index(f'href="/research/{session_id}"'):]
    row = row[:row.index("</tr>")]
    assert f"#{last} Completed" in row
    assert "2 runs" in row and "1 failed" in row
    assert ">1</td>" in row
    assert "Version 1" in row
    assert "Runs: 2, failed: 1" in page


def test_conclusion_page_holds_the_conversation_and_a_card_per_cited_item(conn, authed_client):
    _, c = conn
    session_id = _session(c)
    _run(c, session_id, ResearchRunStatus.COMPLETED)
    item_id = _cited_item_id(c, session_id)
    page = authed_client.get(f"/research/{session_id}").text
    assert 'id="research-conversation-content"' in page
    assert f'data-cite="{item_id}"' in page
    card = page[page.index(f'data-cite-card="{item_id}"'):]
    card = card[:card.index('<div class="cite-card"') if '<div class="cite-card"' in card[1:] else card.index("</section>")]
    assert f'action="/research/{session_id}/evidence/{item_id}/exclude"' in card
    assert 'name="return_to" value="synthesis"' in card
    assert 'href="https://example.com/article" target="_blank"' in card
    assert "A snippet." in card
    # Deleting a session cannot be undone, so the open row asks for the typed word.
    danger = page[page.index('id="delete-session"'):]
    danger = danger[:danger.index("</details>")]
    assert 'name="confirmation"' in danger and "Type &#34;delete&#34; to confirm." in danger
    # The card leads with the title; publisher and quality follow it.
    card = page[page.index(f'data-cite-card="{item_id}"'):]
    assert card.index('class="side-title"') < card.index('class="side-meta"')


@pytest.mark.parametrize("tab, present, absent", [
    ("evidence", "Title and snippet", 'id="research-conversation-content"'),
    ("plan", "The next run searches these sources.", 'id="research-conversation-content"'),
    ("history", "Every run stays here", 'data-cite-card'),
])
def test_each_session_page_shows_only_its_own_part(conn, authed_client, tab, present, absent):
    _, c = conn
    session_id = _session(c)
    _run(c, session_id, ResearchRunStatus.COMPLETED)
    page = authed_client.get(f"/research/{session_id}?tab={tab}").text
    assert present in page
    assert absent not in page


def test_run_history_names_the_failed_step_and_what_finished_runs_made(conn, authed_client):
    _, c = conn
    session_id = _session(c)
    failed = _run(c, session_id, ResearchRunStatus.FAILED, error_code="deadline_exceeded",
                  error_detail="TimeoutError: run deadline passed")
    done = _run(c, session_id, ResearchRunStatus.COMPLETED)
    page = authed_client.get(f"/research/{session_id}?tab=history").text
    failed_row = page[page.index(f"<b>#{failed}</b>"):]
    failed_row = failed_row[:failed_row.index("</tr>")]
    assert "The run hit its time limit before it finished." in failed_row
    assert "TimeoutError: run deadline passed" in failed_row
    done_row = page[page.index(f"<b>#{done}</b>"):]
    done_row = done_row[:done_row.index("</tr>")]
    assert "Snapshot 1: 1 evidence items" in done_row
    assert "Conclusion, version 1" in done_row


@pytest.mark.parametrize("form, location", [
    ({"return_to": "synthesis"}, "/research/{id}"),
    ({"return_to": "evidence", "page": "3"}, "/research/{id}?tab=evidence&page=3"),
    ({}, "/research/{id}?tab=evidence"),
    ({"return_to": "https://example.com/elsewhere"}, "/research/{id}?tab=evidence"),
])
def test_exclude_and_restore_return_to_the_page_they_came_from(conn, authed_client, form, location):
    _, c = conn
    session_id = _session(c)
    _run(c, session_id, ResearchRunStatus.COMPLETED)
    item_id = _cited_item_id(c, session_id)
    expected = location.format(id=session_id)
    for action in ("exclude", "restore"):
        resp = authed_client.post(
            f"/research/{session_id}/evidence/{item_id}/{action}",
            data={"csrf_token": "csrf1", **form})
        assert resp.status_code == 303
        assert resp.headers["location"] == expected


def test_excluding_from_an_archived_session_returns_with_the_error(conn, authed_client):
    _, c = conn
    session_id = _session(c)
    _run(c, session_id, ResearchRunStatus.COMPLETED)
    item_id = _cited_item_id(c, session_id)
    assert authed_client.post(
        f"/research/{session_id}/archive", data={"csrf_token": "csrf1"}).status_code == 303
    resp = authed_client.post(
        f"/research/{session_id}/evidence/{item_id}/exclude",
        data={"csrf_token": "csrf1", "return_to": "synthesis"})
    assert resp.headers["location"] == f"/research/{session_id}?action_error=evidence"


def test_citation_card_says_when_no_excerpt_was_saved(conn, authed_client):
    _, c = conn
    session_id = _session(c)
    _run(c, session_id, ResearchRunStatus.COMPLETED)
    item_id = _cited_item_id(c, session_id)
    c.execute("UPDATE research_evidence_items SET snippet = '' WHERE id = ?", (item_id,))
    c.commit()
    page = authed_client.get(f"/research/{session_id}").text
    start = page.index(f'data-cite-card="{item_id}"')
    card = page[start:page.index("</section>", start)]
    assert "No excerpt was saved for this item. Open the source to read it." in card
    assert "Open the source<" in card


def test_the_new_research_source_picker_shows_keyboard_focus():
    # The picker rows are labels wrapping a checkbox, so focus is drawn on the row itself.
    page = (Path(__file__).parents[2] / "src/beehive/web/templates/research_new.html").read_text()
    css = (Path(__file__).parents[2] / "src/beehive/web/static/admin.css").read_text()
    assert 'class="pick-row"' in page and 'type="checkbox"' in page
    assert ".pick-row:has(input:focus-visible){outline:2px solid var(--link)" in css


def test_deleting_a_session_needs_the_typed_word(conn, authed_client):
    _, c = conn
    session_id = _session(c)
    for typed in ("", "Delete it", "nope"):
        resp = authed_client.post(f"/research/{session_id}/delete",
                                  data={"csrf_token": "csrf1", "confirmation": typed})
        assert resp.headers["location"] == f"/research/{session_id}?action_error=delete"
    assert c.execute("SELECT 1 FROM research_sessions WHERE id = ?", (session_id,)).fetchone()
    resp = authed_client.post(f"/research/{session_id}/delete",
                              data={"csrf_token": "csrf1", "confirmation": "  DELETE "})
    assert resp.headers["location"] == "/research"
    assert c.execute("SELECT 1 FROM research_sessions WHERE id = ?", (session_id,)).fetchone() is None


def test_a_conclusion_is_credited_to_the_run_that_wrote_it(conn, authed_client):
    _, c = conn
    session_id = _session(c)
    first = _run(c, session_id, ResearchRunStatus.COMPLETED)
    # A synthesis-only retry reuses the first run's snapshot and writes version 2.
    later = T0 + timedelta(minutes=5)
    retry = enqueue_research_run(c, session_id, later, run_kind="synthesis")
    lease = claim_research_run(c, retry.id, later, lease_seconds=60, deadline_seconds=1200)
    revision = get_latest_evidence_state_revision(c, session_id)
    item_id = _cited_item_id(c, session_id)
    item_number = c.execute(
        "SELECT citation_number FROM research_evidence_items WHERE id = ?", (item_id,)).fetchone()[0]
    create_synthesis(c, session_id, revision.id, SufficiencyState.SUFFICIENT, (
        SynthesisClaim(
            text="Rates held again", section=SynthesisSection.BOTTOM_LINE,
            provenance=ClaimProvenance.EVIDENCE,
            citations=(EvidenceCitation(evidence_item_id=item_id, citation_number=item_number),)),
    ), "gpt-5", "en", later + timedelta(seconds=10))
    complete_research_run(c, retry.id, lease.run.claim_token, ResearchRunStatus.COMPLETED,
                          later + timedelta(seconds=20))

    page = authed_client.get(f"/research/{session_id}?tab=history").text

    def row(run_id):
        start = page.index(f"<b>#{run_id}</b>")
        return page[start:page.index("</tr>", start)]
    assert "Conclusion, version 2" in row(retry.id)
    assert "Snapshot" not in row(retry.id)
    assert "Conclusion, version 1" in row(first)
    assert "Snapshot 1: 1 evidence items" in row(first)


def test_a_failed_run_still_shows_the_evidence_it_sealed(conn, authed_client):
    _, c = conn
    session_id = _session(c)
    run_id = _run(c, session_id, ResearchRunStatus.FAILED, collect=True,
                  error_code="synthesis_failed")
    page = authed_client.get(f"/research/{session_id}?tab=history").text
    start = page.index(f"<b>#{run_id}</b>")
    row = page[start:page.index("</tr>", start)]
    assert "Writing the conclusion failed." in row
    assert "Snapshot 1: 1 evidence items" in row


def test_older_plans_keep_each_sources_reason(conn, authed_client):
    _, c = conn
    session_id = _session(c)
    run_id = _run(c, session_id, ResearchRunStatus.COMPLETED)
    plan = ('{"plan_summary": "%s", "sources": [{"connector_type": "rbnz_news", "config": {}, '
            '"rationale": "%s"}]}')
    create_plan_revision(c, run_id, plan % ("first", "Official rate decisions"), "a", True, T0)
    create_plan_revision(c, run_id, plan % ("second", "Newest statements"), "b", True, T0)
    page = authed_client.get(f"/research/{session_id}?tab=plan").text
    older = page[page.index('<details class="plan-more">'):]
    assert '<details class="plan-sources">' in older
    assert "Official rate decisions" in older


def test_a_settled_reply_reloads_the_page_and_a_pending_one_keeps_polling(conn, authed_client):
    _, c = conn
    session_id = _session(c)
    _run(c, session_id, ResearchRunStatus.COMPLETED)
    settled = authed_client.get(f"/research/{session_id}/messages/status")
    assert settled.headers.get("HX-Refresh") == "true"
    authed_client.post(f"/research/{session_id}/messages",
                       data={"content": "What changed?", "csrf_token": "csrf1"})
    pending = authed_client.get(f"/research/{session_id}/messages/status")
    assert 'hx-trigger="every 3s"' in pending.text
    assert "HX-Refresh" not in pending.headers


def test_phones_get_session_tabs_and_their_own_select_all(conn, authed_client):
    _, c = conn
    session_id = _session(c)
    page = authed_client.get(f"/research/{session_id}?tab=plan").text
    tabs = page[page.index('<nav class="session-tabs"'):]
    tabs = tabs[:tabs.index("</nav>")]
    assert tabs.count("<a ") == 4
    assert f'<a href="/research/{session_id}?tab=plan" aria-current="page">' in tabs
    _lot(c, "lot")
    watch = authed_client.get("/watchlist").text
    assert '<label class="opt bulk-all-narrow"><input type="checkbox" data-bulk-all="watchlist-bulk-form">' in watch


def test_a_page_past_the_end_shows_the_last_page(conn):
    _, c = conn
    _lot(c, "one")
    _lot(c, "two")
    page = build_watchlist_page(
        c, t=localizer_for("en"), now=datetime.now(timezone.utc),
        query=WatchlistQuery(status="all", page=5, per_page=1))
    assert page.pagination.page == 2
    assert len(page.items) == 1


def test_a_blank_reminder_error_is_not_counted_as_a_failure(conn, authed_client):
    _, c = conn
    _fail_reminder(c, _lot(c, "blank"), " \n\t ")
    assert count_failed_tracker_reminders(c) == 0
    page = authed_client.get("/watchlist").text
    assert "was not sent" not in page
    assert 'id="toc-count-watchlist"></span>' in page
