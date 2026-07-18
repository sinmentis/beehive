import os
from unittest.mock import patch

import pytest

from beehive.collector.research_worker import ReconcileResult
from beehive.db.connection import connect


def _close_coroutine(coroutine):
    coroutine.close()


def _close_coroutine_raising(exc):
    def side_effect(coroutine):
        coroutine.close()
        raise exc
    return side_effect


def test_reconcile_once_mode_runs_sweep_and_exits_zero(tmp_path, monkeypatch):
    db_path = str(tmp_path / "t.db")
    from scripts.run_research_worker import main

    with patch(
        "scripts.run_research_worker.reconcile_once",
        return_value=ReconcileResult(recovered_research_runs=2, recovered_chat_requests=1),
    ) as mock_reconcile:
        code = main(["--db-path", db_path, "--reconcile-once"])

    assert code == 0
    mock_reconcile.assert_called_once()
    # Schema bootstrap really happened -- the DB file is now a valid, queryable Beehive DB.
    conn = connect(db_path)
    conn.execute("SELECT COUNT(*) FROM research_runs").fetchone()
    conn.close()


def test_reconcile_once_mode_exits_nonzero_on_failure(tmp_path, capsys):
    db_path = str(tmp_path / "t.db")
    from scripts.run_research_worker import main

    with patch(
        "scripts.run_research_worker.reconcile_once", side_effect=RuntimeError("db locked"),
    ):
        code = main(["--db-path", db_path, "--reconcile-once"])

    assert code == 1
    assert "db locked" in capsys.readouterr().err


def test_worker_mode_invokes_asyncio_run(tmp_path):
    db_path = str(tmp_path / "t.db")
    from scripts.run_research_worker import main

    with patch(
        "scripts.run_research_worker.asyncio.run", side_effect=_close_coroutine,
    ) as mock_run:
        code = main(["--db-path", db_path])

    assert code == 0
    mock_run.assert_called_once()


def test_invalid_configuration_exits_nonzero_without_running(tmp_path, capsys, monkeypatch):
    db_path = str(tmp_path / "t.db")
    monkeypatch.setenv("RESEARCH_WORKER_RESEARCH_POOL_SIZE", "not-a-number")
    from scripts.run_research_worker import main

    with patch(
        "scripts.run_research_worker.asyncio.run", side_effect=_close_coroutine,
    ) as mock_run:
        code = main(["--db-path", db_path])

    assert code == 1
    mock_run.assert_not_called()
    err = capsys.readouterr().err
    assert "invalid configuration" in err
    assert "not-a-number" in err
    # Never logs the raw environment mapping itself, only the one offending value/key.
    assert "os.environ" not in err


def test_invalid_configuration_never_claims_or_touches_the_database(tmp_path, monkeypatch):
    db_path = str(tmp_path / "t.db")
    monkeypatch.setenv("RESEARCH_WORKER_LEASE_SECONDS", "-5")
    from scripts.run_research_worker import main

    code = main(["--db-path", db_path])
    assert code == 1
    assert not os.path.exists(db_path)


def test_fatal_worker_error_exits_nonzero(tmp_path, capsys):
    db_path = str(tmp_path / "t.db")
    from scripts.run_research_worker import main

    with patch(
        "scripts.run_research_worker.asyncio.run",
        side_effect=_close_coroutine_raising(RuntimeError("boom")),
    ):
        code = main(["--db-path", db_path])

    assert code == 1
    assert "boom" in capsys.readouterr().err


def test_db_path_defaults_to_environment_variable(tmp_path, monkeypatch):
    db_path = str(tmp_path / "env.db")
    monkeypatch.setenv("DB_PATH", db_path)
    from scripts.run_research_worker import main

    with patch(
        "scripts.run_research_worker.asyncio.run", side_effect=_close_coroutine,
    ) as mock_run:
        code = main([])

    assert code == 0
    mock_run.assert_called_once()
    conn = connect(db_path)
    conn.execute("SELECT COUNT(*) FROM research_runs").fetchone()
    conn.close()


@pytest.mark.parametrize("flag", ["--reconcile-once"])
def test_reconcile_once_flag_never_starts_the_long_running_worker(tmp_path, flag):
    db_path = str(tmp_path / "t.db")
    from scripts.run_research_worker import main

    with patch("scripts.run_research_worker.asyncio.run") as mock_run, patch(
        "scripts.run_research_worker.reconcile_once",
        return_value=ReconcileResult(recovered_research_runs=0, recovered_chat_requests=0),
    ):
        code = main(["--db-path", db_path, flag])

    assert code == 0
    mock_run.assert_not_called()
