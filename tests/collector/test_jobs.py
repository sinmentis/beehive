from unittest.mock import AsyncMock, patch

import pytest

from beehive.collector.jobs import run_fetch_job, run_ranking_job
from beehive.connectors.base import RawItem
from beehive.db.channels import create_channel
from beehive.db.connection import connect, init_schema
from beehive.db.items import insert_new
from beehive.db.ranking_requests import claim_next_ranking_request, create_ranking_request
from beehive.db.sources import create_source
from beehive.localization import save_language


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    monkeypatch.delenv("ACS_CONNECTION_STRING", raising=False)
    monkeypatch.setenv("DIGEST_EMAIL_TO", "fallback@example.com")
    path = str(tmp_path / "jobs.db")
    conn = connect(path)
    init_schema(conn)
    conn.close()
    return path


def test_fetch_now_runs_the_channel_forced_in_the_stored_language(db_path):
    conn = connect(db_path)
    channel_id = create_channel(conn, "News", "profile")
    save_language(conn, "ja")
    conn.close()

    with patch("beehive.collector.jobs.run_channel_cycle", new=AsyncMock()) as cycle:
        run_fetch_job(db_path, channel_id, force=True)

    cycle.assert_awaited_once()
    assert cycle.await_args.args[1]["id"] == channel_id
    assert cycle.await_args.kwargs["force_fetch"] is True
    assert cycle.await_args.kwargs["localizer"].code == "ja"
    assert cycle.await_args.kwargs["recipient"] == "fallback@example.com"


def test_a_channel_deleted_before_its_fetch_is_skipped(db_path, capsys):
    with patch("beehive.collector.jobs.run_channel_cycle", new=AsyncMock()) as cycle:
        run_fetch_job(db_path, 999, force=True)

    cycle.assert_not_awaited()
    assert "Channel 999 no longer exists" in capsys.readouterr().out


def test_a_channel_with_an_invalid_alert_address_is_skipped_not_raised(db_path, capsys):
    conn = connect(db_path)
    channel_id = create_channel(conn, "Bad Channel", "profile")
    conn.execute(
        "UPDATE channels SET digest_email = ? WHERE id = ?",
        ("one@example.com,two@example.com", channel_id),
    )
    conn.commit()
    conn.close()

    with patch("beehive.collector.jobs.run_channel_cycle", new=AsyncMock()) as cycle:
        run_fetch_job(db_path, channel_id, force=False)

    cycle.assert_not_awaited()
    assert "Bad Channel" in capsys.readouterr().out


def test_rule_jobs_use_the_workers_stored_language_and_model_without_email_setup(db_path):
    from datetime import datetime, timezone

    conn = connect(db_path)
    channel_id = create_channel(conn, "Rules", "profile")
    source_id = create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "news"})
    insert_new(conn, source_id, RawItem(external_id="a", title="A", url="https://example.com/a"))
    create_ranking_request(conn, channel_id, mode="preview", profile="draft")
    claim = claim_next_ranking_request(conn, datetime.now(timezone.utc), lease_seconds=180)
    save_language(conn, "ja")
    conn.close()

    with (
        patch("beehive.collector.ranking_worker.process_claimed_ranking_request", new=AsyncMock()) as run,
        patch("beehive.collector.jobs.load_model", return_value="offline-model"),
        patch("beehive.collector.jobs.delivery_context") as email,
    ):
        run_ranking_job(db_path, claim)

    run.assert_awaited_once()
    assert run.await_args.args[1] == claim
    assert run.await_args.kwargs["localizer"].code == "ja"
    assert run.await_args.kwargs["model"] == "offline-model"
    email.assert_not_called()
