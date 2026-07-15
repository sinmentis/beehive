import os
import sys
from unittest.mock import AsyncMock, patch

import pytest

from beehive.connectors.registry import register


def _close_coroutine(coroutine):
    coroutine.close()


def _close_coroutine_returning(result):
    def close(coroutine):
        coroutine.close()
        return result
    return close


def _rewrite_result(*, failed=0):
    from beehive.collector.summary_rewrite import SummaryRewriteRunResult
    return SummaryRewriteRunResult(
        run_id="rewrite-1",
        dry_run=False,
        considered=1,
        rewritten=0 if failed else 1,
        already_migrated=0,
        no_longer_eligible=0,
        failed=failed,
        last_item_id=1,
    )


def _rollback_result(*, changed_since=0):
    from beehive.collector.summary_rewrite import SummaryRewriteRollbackResult
    return SummaryRewriteRollbackResult(
        run_id="rewrite-1",
        entries_found=1,
        reverted=0 if changed_since else 1,
        changed_since=changed_since,
    )


def test_fetch_mode_invokes_asyncio_run(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "argv",
                         ["prog", "--mode", "fetch", "--db-path", str(tmp_path / "t.db")])
    with patch("scripts.run_collector.asyncio.run", side_effect=_close_coroutine) as mock_run:
        from scripts.run_collector import main
        main()
    mock_run.assert_called_once()


def test_digest_mode_invokes_run_digest(monkeypatch, tmp_path):
    db_path = str(tmp_path / "t.db")
    monkeypatch.setattr(sys, "argv", ["prog", "--mode", "digest", "--db-path", db_path])
    with patch("scripts.run_collector.run_digest") as mock_digest:
        from scripts.run_collector import main
        main()
    mock_digest.assert_called_once_with(db_path)


def test_deep_read_mode_invokes_asyncio_run(monkeypatch, tmp_path):
    monkeypatch.setattr(
        sys,
        "argv",
        ["prog", "--mode", "deep-read", "--db-path", str(tmp_path / "t.db")],
    )
    with patch("scripts.run_collector.asyncio.run", side_effect=_close_coroutine) as mock_run:
        from scripts.run_collector import main
        main()
    mock_run.assert_called_once()


def test_summary_rewrite_dry_run_mode_invokes_asyncio_run(monkeypatch, tmp_path):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prog",
            "--mode",
            "rewrite-unread-summaries",
            "--db-path",
            str(tmp_path / "t.db"),
            "--run-id",
            "rewrite-1",
            "--high-water-item-id",
            "42",
            "--dry-run",
        ],
    )
    with patch(
        "scripts.run_collector.asyncio.run",
        side_effect=_close_coroutine_returning(_rewrite_result()),
    ) as mock_run:
        from scripts.run_collector import main
        main()
    mock_run.assert_called_once()


@pytest.mark.parametrize(
    "extra_args",
    [
        [],
        ["--run-id", "rewrite-1", "--high-water-item-id", "42"],
        [
            "--run-id",
            "rewrite-1",
            "--high-water-item-id",
            "42",
            "--dry-run",
            "--confirm-rewrite",
        ],
    ],
)
def test_summary_rewrite_mode_requires_explicit_safe_execution_choice(
    monkeypatch,
    tmp_path,
    extra_args,
):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prog",
            "--mode",
            "rewrite-unread-summaries",
            "--db-path",
            str(tmp_path / "t.db"),
            *extra_args,
        ],
    )
    from scripts.run_collector import main

    with pytest.raises(SystemExit):
        main()


def test_summary_rollback_requires_confirmation(monkeypatch, tmp_path):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prog",
            "--mode",
            "rollback-unread-summaries",
            "--db-path",
            str(tmp_path / "t.db"),
            "--run-id",
            "rewrite-1",
        ],
    )
    from scripts.run_collector import main

    with pytest.raises(SystemExit):
        main()


def test_summary_rollback_mode_calls_rollback(monkeypatch, tmp_path):
    db_path = str(tmp_path / "t.db")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prog",
            "--mode",
            "rollback-unread-summaries",
            "--db-path",
            db_path,
            "--run-id",
            "rewrite-1",
            "--confirm-rollback",
        ],
    )
    with patch("scripts.run_collector.run_unread_summary_rollback") as mock_rollback:
        mock_rollback.return_value = _rollback_result()
        from scripts.run_collector import main
        main()

    mock_rollback.assert_called_once_with(db_path, run_id="rewrite-1")


def test_summary_rewrite_mode_exits_nonzero_when_any_item_failed(monkeypatch, tmp_path):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prog",
            "--mode",
            "rewrite-unread-summaries",
            "--db-path",
            str(tmp_path / "t.db"),
            "--run-id",
            "rewrite-1",
            "--high-water-item-id",
            "42",
            "--confirm-rewrite",
        ],
    )

    with patch(
        "scripts.run_collector.asyncio.run",
        side_effect=_close_coroutine_returning(_rewrite_result(failed=1)),
    ):
        from scripts.run_collector import main
        with pytest.raises(SystemExit) as exc_info:
            main()

    assert exc_info.value.code == 1


def test_summary_rollback_mode_exits_nonzero_when_entries_remain(monkeypatch, tmp_path):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prog",
            "--mode",
            "rollback-unread-summaries",
            "--db-path",
            str(tmp_path / "t.db"),
            "--run-id",
            "rewrite-1",
            "--confirm-rollback",
        ],
    )
    with patch(
        "scripts.run_collector.run_unread_summary_rollback",
        return_value=_rollback_result(changed_since=1),
    ):
        from scripts.run_collector import main
        with pytest.raises(SystemExit) as exc_info:
            main()

    assert exc_info.value.code == 1


@pytest.mark.asyncio
async def test_run_deep_read_bootstraps_schema_and_passes_data_dir(tmp_path):
    from beehive.db.connection import connect
    from scripts.run_collector import run_deep_read

    db_path = str(tmp_path / "brand_new.db")
    with patch(
        "scripts.run_collector.process_deep_read_queue",
        new=AsyncMock(),
    ) as mock_process:
        await run_deep_read(db_path)

    conn = connect(db_path)
    assert conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='deep_reads'"
    ).fetchone()
    conn.close()
    mock_process.assert_awaited_once()
    assert mock_process.await_args.args[1] == str(tmp_path)


@pytest.mark.asyncio
async def test_run_unread_summary_rewrite_loads_language_and_prints_result(
    tmp_path,
    capsys,
):
    from beehive.db.connection import connect, init_schema
    from beehive.localization import save_language
    from scripts.run_collector import run_unread_summary_rewrite

    db_path = str(tmp_path / "rewrite.db")
    conn = connect(db_path)
    init_schema(conn)
    save_language(conn, "de")
    conn.close()

    with patch(
        "scripts.run_collector.run_summary_rewrite",
        new=AsyncMock(),
    ) as mock_rewrite:
        from beehive.collector.summary_rewrite import SummaryRewriteRunResult
        mock_rewrite.return_value = SummaryRewriteRunResult(
            run_id="rewrite-1",
            dry_run=True,
            considered=0,
            rewritten=0,
            already_migrated=0,
            no_longer_eligible=0,
            failed=0,
            last_item_id=0,
        )
        result = await run_unread_summary_rewrite(
            db_path,
            high_water_item_id=42,
            run_id="rewrite-1",
            dry_run=True,
        )

    assert mock_rewrite.await_args.args[3].code == "de"
    assert '"run_id": "rewrite-1"' in capsys.readouterr().out
    assert result == mock_rewrite.return_value


@pytest.mark.asyncio
async def test_run_fetch_bootstraps_schema_on_fresh_db(tmp_path, monkeypatch):
    """Regression test: a freshly-deployed beehive-data.volume has no tables until
    something calls init_schema. run_fetch must not crash with "no such table" on a DB
    file that has never been initialized."""
    monkeypatch.delenv("ACS_CONNECTION_STRING", raising=False)
    from scripts.run_collector import run_fetch
    await run_fetch(str(tmp_path / "brand_new.db"))


def test_run_digest_bootstraps_schema_on_fresh_db(tmp_path, monkeypatch):
    """Same regression, for the digest path: send_daily_digest must not crash on a DB
    that's never had init_schema called on it (it needs the channels table to exist)."""
    monkeypatch.delenv("ACS_CONNECTION_STRING", raising=False)
    monkeypatch.setenv("DIGEST_EMAIL_TO", "fallback@example.com")
    from scripts.run_collector import run_digest
    run_digest(str(tmp_path / "brand_new.db"))


@pytest.mark.asyncio
async def test_run_fetch_loads_and_passes_the_stored_platform_language(tmp_path, monkeypatch):
    """The Localizer is loaded once, right after init_schema, and passed explicitly into
    run_channel_cycle -- never read from a process-global."""
    monkeypatch.delenv("ACS_CONNECTION_STRING", raising=False)
    monkeypatch.setenv("DIGEST_EMAIL_TO", "fallback@example.com")
    from beehive.db.channels import create_channel
    from beehive.db.connection import connect, init_schema
    from beehive.localization import save_language
    from scripts.run_collector import run_fetch

    db_path = str(tmp_path / "t.db")
    conn = connect(db_path)
    init_schema(conn)
    save_language(conn, "ja")
    create_channel(conn, "Some Channel", "profile")
    conn.close()

    with patch(
        "scripts.run_collector.run_channel_cycle",
        new=AsyncMock(),
    ) as mock_cycle:
        await run_fetch(db_path)

    localizer = mock_cycle.await_args.kwargs["localizer"]
    assert localizer.code == "ja"
    assert localizer.llm_name == "Japanese"


@pytest.mark.asyncio
async def test_run_fetch_defaults_to_english_when_no_language_is_stored(tmp_path, monkeypatch):
    monkeypatch.delenv("ACS_CONNECTION_STRING", raising=False)
    monkeypatch.setenv("DIGEST_EMAIL_TO", "fallback@example.com")
    from beehive.db.channels import create_channel
    from beehive.db.connection import connect, init_schema
    from scripts.run_collector import run_fetch

    db_path = str(tmp_path / "t.db")
    conn = connect(db_path)
    init_schema(conn)
    create_channel(conn, "Some Channel", "profile")
    conn.close()

    with patch(
        "scripts.run_collector.run_channel_cycle",
        new=AsyncMock(),
    ) as mock_cycle:
        await run_fetch(db_path)

    localizer = mock_cycle.await_args.kwargs["localizer"]
    assert localizer.code == "en"
    assert localizer.llm_name == "English"


@pytest.mark.asyncio
async def test_run_fetch_channel_loads_and_passes_the_stored_platform_language(
        tmp_path, monkeypatch):
    monkeypatch.delenv("ACS_CONNECTION_STRING", raising=False)
    monkeypatch.setenv("DIGEST_EMAIL_TO", "fallback@example.com")
    from beehive.collector.manual_trigger import request_channel_fetch
    from beehive.db.channels import create_channel
    from beehive.db.connection import connect, init_schema
    from beehive.localization import save_language
    from scripts.run_collector import run_fetch_channel

    db_path = str(tmp_path / "t.db")
    conn = connect(db_path)
    init_schema(conn)
    save_language(conn, "de")
    channel_id = create_channel(conn, "Manual Channel", "profile")
    conn.close()

    request_channel_fetch(str(tmp_path), channel_id)
    os.replace(str(tmp_path / "fetch_trigger_channel_id"),
               str(tmp_path / "fetch_trigger_channel_id.inflight"))

    with patch(
        "scripts.run_collector.run_channel_cycle",
        new=AsyncMock(),
    ) as mock_cycle:
        await run_fetch_channel(db_path)

    localizer = mock_cycle.await_args.kwargs["localizer"]
    assert localizer.code == "de"
    assert mock_cycle.await_args.kwargs["force_fetch"] is True


def test_run_digest_loads_and_passes_the_stored_platform_language(tmp_path, monkeypatch):
    monkeypatch.delenv("ACS_CONNECTION_STRING", raising=False)
    monkeypatch.setenv("DIGEST_EMAIL_TO", "fallback@example.com")
    from beehive.db.connection import connect, init_schema
    from beehive.localization import save_language
    from scripts.run_collector import run_digest

    db_path = str(tmp_path / "t.db")
    conn = connect(db_path)
    init_schema(conn)
    save_language(conn, "fr")
    conn.close()

    with patch("scripts.run_collector.send_daily_digest") as mock_send:
        run_digest(db_path)

    localizer = mock_send.call_args.args[-1]
    assert localizer.code == "fr"


class _ManualTriggerStubConnector:
    type_key = "manual_trigger_stub"

    def validate_config(self, config):
        pass

    def fetch(self, config):
        return []


def test_fetch_channel_mode_invokes_asyncio_run(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "argv",
                         ["prog", "--mode", "fetch-channel", "--db-path", str(tmp_path / "t.db")])
    with patch("scripts.run_collector.asyncio.run", side_effect=_close_coroutine) as mock_run:
        from scripts.run_collector import main
        main()
    mock_run.assert_called_once()


@pytest.mark.asyncio
async def test_run_fetch_channel_processes_only_the_requested_channel(tmp_path, monkeypatch):
    monkeypatch.delenv("ACS_CONNECTION_STRING", raising=False)
    from beehive.collector.manual_trigger import request_channel_fetch
    from beehive.db.channels import create_channel
    from beehive.db.connection import connect, init_schema
    from beehive.db.sources import create_source, record_fetch_success
    from beehive.db.sources import list_by_channel as list_sources
    from scripts.run_collector import run_fetch_channel

    register(_ManualTriggerStubConnector())

    db_path = str(tmp_path / "t.db")
    conn = connect(db_path)
    init_schema(conn)
    target_id = create_channel(conn, "Target", "target profile")
    other_id = create_channel(conn, "Other", "other profile")
    previous_fetch = "2099-01-01T00:00:00+00:00"
    target_source_id = create_source(
        conn,
        target_id,
        "manual_trigger_stub",
        {},
    )
    record_fetch_success(conn, target_source_id, previous_fetch)
    create_source(conn, other_id, "manual_trigger_stub", {})
    conn.close()

    request_channel_fetch(str(tmp_path), target_id)
    os.replace(str(tmp_path / "fetch_trigger_channel_id"),
               str(tmp_path / "fetch_trigger_channel_id.inflight"))

    await run_fetch_channel(db_path)

    conn2 = connect(db_path)
    assert list_sources(conn2, target_id)[0]["last_fetch_at"] != previous_fetch
    assert list_sources(conn2, other_id)[0]["last_fetch_at"] is None


@pytest.mark.asyncio
async def test_run_fetch_channel_is_a_noop_with_no_marker_present(tmp_path, monkeypatch):
    monkeypatch.delenv("ACS_CONNECTION_STRING", raising=False)
    from beehive.db.channels import create_channel
    from beehive.db.connection import connect, init_schema
    from scripts.run_collector import run_fetch_channel

    db_path = str(tmp_path / "t.db")
    conn = connect(db_path)
    init_schema(conn)
    create_channel(conn, "Some Channel", "profile")
    conn.close()

    await run_fetch_channel(db_path)  # no marker file at all -- must not raise


@pytest.mark.asyncio
async def test_run_fetch_channel_is_a_noop_when_the_channel_no_longer_exists(tmp_path, monkeypatch):
    monkeypatch.delenv("ACS_CONNECTION_STRING", raising=False)
    from beehive.collector.manual_trigger import request_channel_fetch
    from beehive.db.connection import connect, init_schema
    from scripts.run_collector import run_fetch_channel

    db_path = str(tmp_path / "t.db")
    conn = connect(db_path)
    init_schema(conn)
    conn.close()

    request_channel_fetch(str(tmp_path), 999)  # no Channel 999 exists
    os.replace(str(tmp_path / "fetch_trigger_channel_id"),
               str(tmp_path / "fetch_trigger_channel_id.inflight"))

    await run_fetch_channel(db_path)  # must not raise


@pytest.mark.asyncio
async def test_run_fetch_passes_each_channels_effective_recipient(tmp_path, monkeypatch):
    monkeypatch.delenv("ACS_CONNECTION_STRING", raising=False)
    monkeypatch.setenv("DIGEST_EMAIL_TO", "fallback@example.com")
    from beehive.db import app_state
    from beehive.db.channels import create_channel, update_channel
    from beehive.db.connection import connect, init_schema
    from scripts.run_collector import run_fetch

    db_path = str(tmp_path / "routing.db")
    conn = connect(db_path)
    init_schema(conn)
    app_state.set(conn, "default_digest_email", "default@example.com")
    inherited = create_channel(conn, "Inherited", "profile")
    overridden = create_channel(conn, "Overridden", "profile")
    update_channel(
        conn, overridden, "Overridden", "profile",
        fetch_interval_hours=3, digest_email="channel@example.com")
    conn.close()

    with patch(
        "scripts.run_collector.run_channel_cycle",
        new=AsyncMock(),
    ) as mock_cycle:
        await run_fetch(db_path)

    recipients = {
        call.args[1]["id"]: call.kwargs["recipient"]
        for call in mock_cycle.await_args_list
    }
    assert recipients == {
        inherited: "default@example.com",
        overridden: "channel@example.com",
    }
    assert all(
        call.kwargs.get("force_fetch") is not True
        for call in mock_cycle.await_args_list
    )
    # the Localizer is loaded once and passed explicitly into every Channel's cycle, never
    # read again from a process-global.
    localizers = {call.kwargs["localizer"] for call in mock_cycle.await_args_list}
    assert len(localizers) == 1
    assert next(iter(localizers)).code == "en"


@pytest.mark.asyncio
async def test_run_fetch_skips_channel_with_invalid_override_and_continues(
        tmp_path, monkeypatch, capsys):
    """A single Channel with a malformed override must not abort the whole fetch run:
    other Channels still get processed and the bad one is logged and skipped."""
    monkeypatch.delenv("ACS_CONNECTION_STRING", raising=False)
    monkeypatch.setenv("DIGEST_EMAIL_TO", "fallback@example.com")
    from beehive.db.channels import create_channel
    from beehive.db.connection import connect, init_schema
    from scripts.run_collector import run_fetch

    db_path = str(tmp_path / "routing.db")
    conn = connect(db_path)
    init_schema(conn)
    good = create_channel(conn, "Good Channel", "profile")
    bad = create_channel(conn, "Bad Channel", "profile")
    conn.execute(
        "UPDATE channels SET digest_email = ? WHERE id = ?",
        ("one@example.com,two@example.com", bad))
    conn.commit()
    conn.close()

    with patch(
        "scripts.run_collector.run_channel_cycle",
        new=AsyncMock(),
    ) as mock_cycle:
        await run_fetch(db_path)

    processed = {call.args[1]["id"] for call in mock_cycle.await_args_list}
    assert processed == {good}
    assert "Bad Channel" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_run_fetch_isolates_alert_delivery_config_error_and_still_raises(
        tmp_path, monkeypatch, capsys):
    """An EmailConfigurationError raised by run_channel_cycle (e.g. an LLM-failure alert
    with no recipient configured) must not abort the whole run: later Channels are still
    attempted, and the run still fails afterwards with an ExceptionGroup carrying the error."""
    monkeypatch.delenv("ACS_CONNECTION_STRING", raising=False)
    monkeypatch.setenv("DIGEST_EMAIL_TO", "fallback@example.com")
    from beehive.db.channels import create_channel
    from beehive.db.connection import connect, init_schema
    from beehive.email_routing import EmailConfigurationError
    from scripts.run_collector import run_fetch

    db_path = str(tmp_path / "routing.db")
    conn = connect(db_path)
    init_schema(conn)
    create_channel(conn, "First Channel", "profile")
    create_channel(conn, "Second Channel", "profile")
    conn.close()

    config_error = EmailConfigurationError("No email recipient is configured")

    def cycle(conn, channel, notifier, *, recipient=None, localizer=None):
        if channel["name"] == "First Channel":
            raise config_error

    with patch(
        "scripts.run_collector.run_channel_cycle",
        new=AsyncMock(side_effect=cycle),
    ) as mock_cycle:
        with pytest.raises(ExceptionGroup) as excinfo:
            await run_fetch(db_path)

    processed = {call.args[1]["name"] for call in mock_cycle.await_args_list}
    assert processed == {"First Channel", "Second Channel"}
    assert config_error in excinfo.value.exceptions
    assert "First Channel" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_run_fetch_channel_logs_and_reraises_alert_delivery_config_error(
        tmp_path, monkeypatch, capsys):
    """The manual single-Channel path must surface an alert-delivery configuration error:
    log the Channel and re-raise so the fetch-channel unit fails explicitly."""
    monkeypatch.delenv("ACS_CONNECTION_STRING", raising=False)
    monkeypatch.setenv("DIGEST_EMAIL_TO", "fallback@example.com")
    from beehive.collector.manual_trigger import request_channel_fetch
    from beehive.db.channels import create_channel
    from beehive.db.connection import connect, init_schema
    from beehive.email_routing import EmailConfigurationError
    from scripts.run_collector import run_fetch_channel

    db_path = str(tmp_path / "t.db")
    conn = connect(db_path)
    init_schema(conn)
    channel_id = create_channel(conn, "Manual Channel", "profile")
    conn.close()

    request_channel_fetch(str(tmp_path), channel_id)
    os.replace(str(tmp_path / "fetch_trigger_channel_id"),
               str(tmp_path / "fetch_trigger_channel_id.inflight"))

    config_error = EmailConfigurationError("No email recipient is configured")

    def cycle(
        conn,
        channel,
        notifier,
        *,
        recipient=None,
        localizer=None,
        force_fetch=False,
    ):
        assert force_fetch is True
        raise config_error

    with patch(
        "scripts.run_collector.run_channel_cycle",
        new=AsyncMock(side_effect=cycle),
    ):
        with pytest.raises(EmailConfigurationError):
            await run_fetch_channel(db_path)

    assert "Manual Channel" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_run_fetch_channel_skips_invalid_override_without_raising(
        tmp_path, monkeypatch, capsys):
    """The manual single-Channel path must log and return on a malformed override rather
    than crashing the fetch-channel unit."""
    monkeypatch.delenv("ACS_CONNECTION_STRING", raising=False)
    monkeypatch.setenv("DIGEST_EMAIL_TO", "fallback@example.com")
    from beehive.collector.manual_trigger import request_channel_fetch
    from beehive.db.channels import create_channel
    from beehive.db.connection import connect, init_schema
    from scripts.run_collector import run_fetch_channel

    db_path = str(tmp_path / "t.db")
    conn = connect(db_path)
    init_schema(conn)
    channel_id = create_channel(conn, "Bad Channel", "profile")
    conn.execute(
        "UPDATE channels SET digest_email = ? WHERE id = ?",
        ("one@example.com,two@example.com", channel_id))
    conn.commit()
    conn.close()

    request_channel_fetch(str(tmp_path), channel_id)
    os.replace(str(tmp_path / "fetch_trigger_channel_id"),
               str(tmp_path / "fetch_trigger_channel_id.inflight"))

    with patch(
        "scripts.run_collector.run_channel_cycle",
        new=AsyncMock(),
    ) as mock_cycle:
        await run_fetch_channel(db_path)

    mock_cycle.assert_not_awaited()
    assert "Bad Channel" in capsys.readouterr().out


def test_collector_registers_both_hackernews_source_types():
    import scripts.run_collector  # noqa: F401
    from beehive.connectors.registry import get

    assert get("hackernews_stories").type_key == "hackernews_stories"
    assert get("hackernews_query").type_key == "hackernews_query"


def test_official_feed_connectors_are_registered_for_the_collector():
    import scripts.run_collector  # noqa: F401
    from beehive.connectors.registry import get

    for type_key in ("rbnz_news", "nz_government_news", "federal_reserve_news"):
        assert get(type_key).type_key == type_key
