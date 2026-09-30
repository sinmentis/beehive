from unittest.mock import AsyncMock, patch

import pytest

from beehive.collector.jobs import run_fetch_job
from beehive.db.channels import create_channel
from beehive.db.connection import connect, init_schema
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
