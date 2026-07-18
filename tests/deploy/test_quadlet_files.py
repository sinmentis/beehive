# tests/deploy/test_quadlet_files.py
"""Static syntax validation for deploy/quadlet/*.container and *.timer — catches the
"Quadlet silently generates nothing" class of bug (e.g. a [Service]-only key placed under
[Container]) without needing a real Podman/systemd host."""
from __future__ import annotations

import configparser
from pathlib import Path

_QUADLET_DIR = Path(__file__).parent.parent.parent / "deploy" / "quadlet"
_SERVICE_ONLY_KEYS = {"CPUQuota", "CPUWeight", "MemoryHigh", "MemoryMax", "TimeoutStartSec",
                      "TimeoutStopSec", "ExecStartPre", "Type", "Restart"}


def _parser() -> configparser.ConfigParser:
    # interpolation=None: systemd specifiers like %h (user home dir) are literal characters in
    # a Quadlet file, not configparser's own %-interpolation syntax -- without this, any file
    # containing %h/%t/etc. (a common systemd idiom for host-side paths) raises
    # InterpolationSyntaxError the moment a value containing it is accessed (e.g.
    # parser["Service"]["ExecStartPre"]), even though it's perfectly valid to systemd itself.
    p = configparser.ConfigParser(strict=False, interpolation=None)
    p.optionxform = str  # systemd/Quadlet keys are case-sensitive; don't lowercase them
    return p


def test_expected_container_files_exist():
    names = {f.name for f in _QUADLET_DIR.glob("*.container")}
    assert {"beehive-web.container", "beehive-fetch.container",
            "beehive-digest.container", "beehive-deep-read.container",
            "beehive-research.container", "beehive-research-reconcile.container"} <= names


def test_expected_volume_files_exist():
    names = {f.name for f in _QUADLET_DIR.glob("*.volume")}
    assert "beehive-data.volume" in names


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
        assert not leaked, f"{path.name}: [Service]-only keys under [Container]: {leaked}"


def test_oneshot_containers_have_no_install_section():
    for name in (
        "beehive-fetch.container",
        "beehive-digest.container",
        "beehive-deep-read.container",
        "beehive-research-reconcile.container",
    ):
        parser = _parser()
        parser.read(_QUADLET_DIR / name)
        assert "Install" not in parser, f"{name}: oneshot units are timer-started, not boot-started"


def test_timer_files_are_valid_ini_and_reference_a_unit():
    for path in _QUADLET_DIR.glob("*.timer"):
        parser = _parser()
        parser.read(path)
        assert "Timer" in parser, f"{path.name}: missing [Timer] section"
        assert "OnCalendar" in parser["Timer"], f"{path.name}: missing OnCalendar"
        assert "Unit" in parser["Timer"], f"{path.name}: missing Unit= reference"


def test_timer_files_reference_a_service_with_a_matching_container_file():
    # Catches the actual bug found while writing this plan: a .timer file's Unit= line is
    # never validated against reality by configparser alone (it's just a string), so a stale
    # reference to a since-renamed .container file's generated service name would silently
    # daemon-reload without error and simply never fire the intended service again.
    container_stems = {f.stem for f in _QUADLET_DIR.glob("*.container")}
    for path in _QUADLET_DIR.glob("*.timer"):
        parser = _parser()
        parser.read(path)
        unit = parser["Timer"]["Unit"]
        assert unit.endswith(".service"), f"{path.name}: Unit= '{unit}' should end in .service"
        stem = unit.removesuffix(".service")
        assert stem in container_stems, (
            f"{path.name}: Unit={unit} has no matching {stem}.container file"
        )


def test_web_container_has_session_secret():
    parser = _parser()
    parser.read(_QUADLET_DIR / "beehive-web.container")
    secret_line = parser["Container"]["Secret"]
    assert "target=SESSION_SECRET" in secret_line


def test_web_container_has_digest_email_fallback():
    content = (_QUADLET_DIR / "beehive-web.container").read_text()
    assert "Environment=DIGEST_EMAIL_TO=you@example.com" in content


def test_expected_path_unit_exists():
    names = {f.name for f in _QUADLET_DIR.glob("*.path")}
    assert {"beehive-fetch-manual.path", "beehive-deep-read.path"} <= names


def test_manual_fetch_container_exists_and_has_no_install_section():
    parser = _parser()
    parser.read(_QUADLET_DIR / "beehive-fetch-manual.container")
    assert "Container" in parser, "missing [Container] section"
    assert "Install" not in parser, "path-started units are not boot-started"


def test_manual_fetch_container_has_the_copilot_secret_and_db_path():
    parser = _parser()
    parser.read(_QUADLET_DIR / "beehive-fetch-manual.container")
    assert "target=COPILOT_GITHUB_TOKEN" in parser["Container"]["Secret"]
    assert parser["Container"]["Environment"] == "DB_PATH=/data/beehive.db"


def test_manual_fetch_container_execstartpre_consumes_the_correct_marker():
    parser = _parser()
    parser.read(_QUADLET_DIR / "beehive-fetch-manual.container")
    exec_start_pre = parser["Service"]["ExecStartPre"]
    assert "fetch_trigger_channel_id" in exec_start_pre
    assert "fetch_trigger_channel_id.inflight" in exec_start_pre


def test_manual_fetch_path_unit_watches_the_correct_marker_and_references_the_manual_service():
    parser = _parser()
    parser.read(_QUADLET_DIR / "beehive-fetch-manual.path")
    assert "Path" in parser, "missing [Path] section"
    assert parser["Path"]["PathExists"].endswith("fetch_trigger_channel_id")
    assert parser["Path"]["Unit"] == "beehive-fetch-manual.service"


def test_deep_read_container_has_secret_limits_and_reconciliation_units():
    parser = _parser()
    parser.read(_QUADLET_DIR / "beehive-deep-read.container")

    assert "target=COPILOT_GITHUB_TOKEN" in parser["Container"]["Secret"]
    assert parser["Container"]["Environment"] == "DB_PATH=/data/beehive.db"
    assert parser["Container"]["Exec"] == "-m scripts.run_collector --mode deep-read"
    assert parser["Service"]["Restart"] == "on-failure"
    assert parser["Service"]["TimeoutStartSec"] == "1800"
    assert "deep_read_trigger.inflight" in parser["Service"]["ExecStartPre"]

    path_parser = _parser()
    path_parser.read(_QUADLET_DIR / "beehive-deep-read.path")
    assert path_parser["Path"]["PathExists"].endswith("deep_read_trigger")
    assert path_parser["Path"]["Unit"] == "beehive-deep-read.service"

    timer_parser = _parser()
    timer_parser.read(_QUADLET_DIR / "beehive-deep-read.timer")
    assert timer_parser["Timer"]["Unit"] == "beehive-deep-read.service"


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


def test_research_reconcile_container_is_oneshot_without_copilot_secret():
    parser = _parser()
    parser.read(_QUADLET_DIR / "beehive-research-reconcile.container")

    assert parser["Container"]["Exec"] == "-m scripts.run_research_worker --reconcile-once"
    assert parser["Container"]["Volume"] == "beehive-data.volume:/data"
    assert parser["Container"]["Environment"] == "DB_PATH=/data/beehive.db"
    # Reconciliation only recovers expired leases -- it never calls the AI, so it does not need
    # (and should not receive) the Copilot GitHub token secret the always-on worker requires.
    assert "Secret" not in parser["Container"]
    assert parser["Service"]["Type"] == "oneshot"
    assert "Install" not in parser, "oneshot units are timer-started, not boot-started"


def test_research_reconcile_timer_references_the_reconcile_service():
    parser = _parser()
    parser.read(_QUADLET_DIR / "beehive-research-reconcile.timer")
    assert parser["Timer"]["Unit"] == "beehive-research-reconcile.service"


def test_containerfile_import_smoke_test_includes_research_worker():
    containerfile = (_QUADLET_DIR.parent.parent / "Containerfile").read_text()
    assert "scripts.run_research_worker" in containerfile
