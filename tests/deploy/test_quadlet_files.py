# tests/deploy/test_quadlet_files.py
"""Static syntax validation for deploy/quadlet/*.container and *.timer — catches the
"Quadlet silently generates nothing" class of bug (e.g. a [Service]-only key placed under
[Container]) without needing a real Podman/systemd host."""

from __future__ import annotations

import configparser
from pathlib import Path

_QUADLET_DIR = Path(__file__).parent.parent.parent / "deploy" / "quadlet"
_SERVICE_ONLY_KEYS = {
    "CPUQuota",
    "CPUWeight",
    "MemoryHigh",
    "MemoryMax",
    "TimeoutStartSec",
    "TimeoutStopSec",
    "ExecStartPre",
    "Type",
    "Restart",
}


def _parser() -> configparser.ConfigParser:
    # interpolation=None: systemd specifiers like %h (user home dir) are literal characters in
    # a Quadlet file, not configparser's own %-interpolation syntax -- without this, any file
    # containing %h/%t/etc. (a common systemd idiom for host-side paths) raises
    # InterpolationSyntaxError the moment a value containing it is accessed (e.g.
    # parser["Service"]["ExecStartPre"]), even though it's perfectly valid to systemd itself.
    p = configparser.ConfigParser(strict=False, interpolation=None)
    p.optionxform = str  # systemd/Quadlet keys are case-sensitive; don't lowercase them
    return p


def test_expected_units_are_exactly_the_web_app_and_two_workers():
    """ADR-0012: every background job runs in the always-on jobs worker, so there are no timers,
    path units or oneshot containers left to install."""
    assert {f.name for f in _QUADLET_DIR.iterdir() if f.is_file()} == {
        "beehive-data.volume",
        "beehive-web.container",
        "beehive-research.container",
        "beehive-jobs.container",
    }


def test_container_volume_references_have_a_matching_volume_file():
    # Catches the actual bug found before first deploy: all three .container files
    # reference "newscenter-data.volume", but no such Quadlet unit existed in this
    # directory, so `systemctl daemon-reload` would have failed at install time.
    volume_files = {f.name for f in _QUADLET_DIR.glob("*.volume")}
    for path in _QUADLET_DIR.glob("*.container"):
        parser = _parser()
        parser.read(path)
        volume_line = parser["Container"].get("Volume")
        if volume_line is None:
            continue
        source = volume_line.split(":", 1)[0]
        assert source in volume_files, (
            f"{path.name}: Volume= references '{source}', but no matching .volume file exists"
        )


def test_container_files_are_valid_ini_with_no_service_keys_under_container():
    for path in _QUADLET_DIR.glob("*.container"):
        parser = _parser()
        parser.read(path)
        assert "Container" in parser, f"{path.name}: missing [Container] section"
        leaked = _SERVICE_ONLY_KEYS & set(parser["Container"].keys())
        assert not leaked, (
            f"{path.name}: [Service]-only keys under [Container]: {leaked}"
        )


def test_every_container_is_always_on_and_boot_wanted():
    for path in _QUADLET_DIR.glob("*.container"):
        parser = _parser()
        parser.read(path)
        assert "Type" not in parser["Service"] or parser["Service"]["Type"] != "oneshot", path.name
        assert parser["Install"]["WantedBy"] == "default.target", path.name


def test_web_container_has_session_secret():
    # Read the raw text rather than through configparser: systemd allows a key to repeat (each
    # `Secret=` line adds one secret), but configparser keeps only the last occurrence.
    content = (_QUADLET_DIR / "beehive-web.container").read_text()
    assert "target=SESSION_SECRET" in content


def test_web_container_has_digest_email_fallback():
    content = (_QUADLET_DIR / "beehive-web.container").read_text()
    assert "Environment=DIGEST_EMAIL_TO=you@example.com" in content


def test_every_container_that_can_send_email_carries_the_acs_secret():
    """The admin UI's Email Group "Test send" route calls the same `build_notifier` the jobs
    worker does, so the web container needs the same credential. It did not have it, and
    `build_notifier` falls back to logging instead of failing, so a test send silently did
    nothing while the digest it was previewing would have delivered fine."""
    for name in ("beehive-web.container", "beehive-jobs.container"):
        content = (_QUADLET_DIR / name).read_text()
        assert "target=ACS_CONNECTION_STRING" in content, f"{name}: no ACS secret"
        assert "DIGEST_EMAIL_FROM=" in content, f"{name}: no sender address"


def test_jobs_container_runs_the_worker_with_both_secrets_and_room_to_stop():
    content = (_QUADLET_DIR / "beehive-jobs.container").read_text()
    parser = _parser()
    parser.read(_QUADLET_DIR / "beehive-jobs.container")

    assert parser["Container"]["Exec"] == "-m scripts.run_jobs"
    assert parser["Container"]["Volume"] == "beehive-data.volume:/data"
    assert "Environment=DB_PATH=/data/beehive.db" in content
    assert "target=COPILOT_GITHUB_TOKEN" in content
    assert parser["Service"]["Restart"] == "always"
    # The worker waits 30 s for running jobs, then hands their claims back.
    assert int(parser["Service"]["TimeoutStopSec"]) > 30


def test_research_container_is_always_on_with_secret_limits_and_install():
    parser = _parser()
    parser.read(_QUADLET_DIR / "beehive-research.container")

    assert "target=COPILOT_GITHUB_TOKEN" in parser["Container"]["Secret"]
    assert parser["Container"]["Environment"] == "DB_PATH=/data/beehive.db"
    assert parser["Container"]["Exec"] == "-m scripts.run_research_worker"
    assert parser["Container"]["Volume"] == "beehive-data.volume:/data"
    assert parser["Service"]["Restart"] == "always"
    assert int(parser["Service"]["TimeoutStopSec"]) > 0
    assert "Install" in parser, "always-on unit must be boot-wanted"
    assert parser["Install"]["WantedBy"] == "default.target"


def test_containerfile_import_smoke_test_includes_both_workers():
    containerfile = (_QUADLET_DIR.parent.parent / "Containerfile").read_text()
    assert "scripts.run_research_worker" in containerfile
    assert "scripts.run_jobs" in containerfile


def test_release_restarts_every_always_on_unit():
    release = (_QUADLET_DIR.parent / "release.sh").read_text()
    units = {f.stem + ".service" for f in _QUADLET_DIR.glob("*.container")}
    line = next(line for line in release.splitlines() if line.startswith("ALWAYS_ON_UNITS="))
    assert set(line.split("(", 1)[1].rstrip(")").split()) == units
