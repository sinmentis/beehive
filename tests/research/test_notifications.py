from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from beehive.db.connection import connect, init_schema
from beehive.db.research_runs import enqueue_research_run
from beehive.db.research_sessions import create_research_session
from beehive.email_routing import ResolvedRecipient
from beehive.localization import localizer_for
from beehive.research.notifications import (
    ResearchCompletionDeliveryError,
    send_research_completion_notifications,
)

T0 = datetime(2026, 7, 15, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    connection = connect(str(tmp_path / "test.db"))
    init_schema(connection)
    return connection


def _complete_run(conn, question: str = "What changed?") -> tuple[int, int]:
    session_id = create_research_session(conn, question, T0).id
    run_id = enqueue_research_run(conn, session_id, T0).id
    conn.execute(
        "UPDATE research_runs SET status = 'completed', completed_at = ? WHERE id = ?",
        ((T0 + timedelta(minutes=5)).isoformat(), run_id),
    )
    conn.commit()
    return session_id, run_id


def test_completion_notification_sends_once_and_records_success(conn):
    session_id, run_id = _complete_run(conn)
    notifier = MagicMock()
    recipient = ResolvedRecipient("owner@example.com", "database")

    sent = send_research_completion_notifications(
        conn,
        notifier,
        recipient,
        localizer_for("en"),
        now=T0 + timedelta(minutes=6),
    )

    assert sent == 1
    notifier.send.assert_called_once_with(
        "Research complete: What changed?",
        (
            'Research for "What changed?" is ready.\n\nOpen Beehive Research session '
            f"{session_id} to review the synthesis and evidence."
        ),
        to_addr="owner@example.com",
    )
    notification = conn.execute(
        "SELECT attempted_at, sent_at FROM research_completion_notifications WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    assert notification["attempted_at"] == (T0 + timedelta(minutes=6)).isoformat()
    assert notification["sent_at"] == (T0 + timedelta(minutes=6)).isoformat()

    assert send_research_completion_notifications(
        conn,
        notifier,
        recipient,
        localizer_for("en"),
        now=T0 + timedelta(minutes=7),
    ) == 0
    notifier.send.assert_called_once()


def test_failed_completion_notification_remains_retryable(conn):
    _, run_id = _complete_run(conn)
    notifier = MagicMock()
    notifier.send.side_effect = RuntimeError("delivery unavailable")
    attempted_at = T0 + timedelta(minutes=6)

    with pytest.raises(ExceptionGroup, match="Research completion emails failed") as exc_info:
        send_research_completion_notifications(
            conn,
            notifier,
            ResolvedRecipient("owner@example.com", "database"),
            localizer_for("en"),
            now=attempted_at,
        )
    failure = exc_info.value.exceptions[0]
    assert isinstance(failure, ResearchCompletionDeliveryError)
    assert failure.run_id == run_id
    assert str(failure.error) == "delivery unavailable"

    notification = conn.execute(
        "SELECT attempted_at, sent_at FROM research_completion_notifications WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    assert notification["attempted_at"] == attempted_at.isoformat()
    assert notification["sent_at"] is None

    notifier.send.side_effect = None
    assert send_research_completion_notifications(
        conn,
        notifier,
        ResolvedRecipient("owner@example.com", "database"),
        localizer_for("en"),
        now=T0 + timedelta(minutes=7),
    ) == 1


def test_one_failed_completion_email_does_not_block_later_runs(conn):
    _, failed_run_id = _complete_run(conn, "First question")
    _, sent_run_id = _complete_run(conn, "Second question")
    notifier = MagicMock()
    notifier.send.side_effect = [RuntimeError("first failed"), None]

    with pytest.raises(ExceptionGroup):
        send_research_completion_notifications(
            conn,
            notifier,
            ResolvedRecipient("owner@example.com", "database"),
            localizer_for("en"),
            now=T0 + timedelta(minutes=6),
        )

    assert notifier.send.call_count == 2
    notifications = {
        row["run_id"]: row["sent_at"]
        for row in conn.execute(
            "SELECT run_id, sent_at FROM research_completion_notifications"
        )
    }
    assert notifications[failed_run_id] is None
    assert notifications[sent_run_id] == (T0 + timedelta(minutes=6)).isoformat()


def test_completion_notification_waits_for_a_configured_recipient(conn):
    _, run_id = _complete_run(conn)
    notifier = MagicMock()

    assert send_research_completion_notifications(
        conn,
        notifier,
        ResolvedRecipient(None, "missing"),
        localizer_for("en"),
    ) == 0
    notifier.send.assert_not_called()
    assert conn.execute(
        "SELECT 1 FROM research_completion_notifications WHERE run_id = ?",
        (run_id,),
    ).fetchone() is None
