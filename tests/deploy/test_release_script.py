"""Drives deploy/release.sh against a fake podman that keeps image tags in a JSON file, so tag
moves during promote and prune are checked without touching real images or units."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "release.sh"
_IMAGE = "localhost/beehive"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

_FAKE_PODMAN = textwrap.dedent(
    """
    import json, sys
    state_path = sys.argv[1]
    args = sys.argv[2:]
    with open(state_path) as handle:
        state = json.load(handle)
    tags, images = state["tags"], state["images"]

    def resolve(ref):
        return tags.get(ref) or (ref if ref in images else None)

    def save():
        with open(state_path, "w") as handle:
            json.dump(state, handle)

    if args[0] == "build":
        state["build_args"] = args[1:]
        save()
    elif args[:2] == ["image", "exists"]:
        sys.exit(0 if resolve(args[2]) else 1)
    elif args[:2] == ["image", "inspect"]:
        image_id = resolve(args[2])
        if image_id is None:
            sys.exit(125)
        print(image_id)
    elif args[0] == "tag":
        tags[args[2]] = resolve(args[1])
        save()
    elif args[0] == "untag":
        del tags[args[1]]
        save()
    elif args[0] == "images":
        for ref, image_id in sorted(tags.items()):
            if ref.startswith(args[1] + ":"):
                print(f"{ref} sha256:{image_id}")
    elif args[:2] == ["image", "rm"]:
        if args[2] in tags.values():
            sys.exit(2)
        images.remove(args[2])
        save()
    else:
        sys.exit(f"unexpected podman call: {args}")
    """
)


@pytest.fixture
def harness(tmp_path):
    state = tmp_path / "podman.json"
    fake = tmp_path / "fake_podman.py"
    fake.write_text(_FAKE_PODMAN)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    podman = bin_dir / "podman"
    podman.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{fake}" "{state}" "$@"\n')
    calls = tmp_path / "systemctl.log"
    systemctl = bin_dir / "systemctl"
    systemctl.write_text(f'#!/bin/sh\necho "$@" >> "{calls}"\n')
    curl = bin_dir / "curl"
    curl.write_text("#!/bin/sh\nexit 0\n")
    for tool in (podman, systemctl, curl):
        tool.chmod(0o755)

    def setup(tags: dict[str, str]) -> None:
        state.write_text(json.dumps({
            "tags": {f"{_IMAGE}:{tag}": image_id for tag, image_id in tags.items()},
            "images": sorted(set(tags.values()) | {"orphan"}),
        }))

    def run(*args: str) -> subprocess.CompletedProcess:
        env = {
            **os.environ,
            # Fakes first on PATH too, so even a direct `podman` call can never reach the host.
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "BEEHIVE_PODMAN": str(podman),
            "BEEHIVE_SYSTEMCTL": str(systemctl),
            "BEEHIVE_CURL": str(curl),
            "BEEHIVE_SETTLE_SECONDS": "0",
        }
        return subprocess.run(
            ["bash", str(_SCRIPT), *args], env=env, capture_output=True, text=True,
            check=False)

    def tags() -> dict[str, str]:
        stored = json.loads(state.read_text())["tags"]
        return {ref.split(":", 1)[1]: image_id for ref, image_id in stored.items()}

    def images() -> list[str]:
        return json.loads(state.read_text())["images"]

    return setup, run, tags, images, calls


def test_build_tags_the_package_version_and_keeps_commit_provenance(harness):
    setup, run, _, _, calls = harness
    setup({})
    revision = "abc1234" + "0" * 33
    git = calls.parent / "bin" / "git"
    git.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-C" ]; then shift 2; fi\n'
        'case "$*" in\n'
        '  "status --porcelain") exit 0;;\n'
        '  "tag --list v*") exit 0;;\n'
        f'  "rev-parse HEAD") echo "{revision}";;\n'
        f'  "rev-parse --short=7 HEAD") echo "{revision[:7]}";;\n'
        "  *) exit 1;;\n"
        "esac\n"
    )
    git.chmod(0o755)

    result = run("build")

    assert result.returncode == 0, result.stderr
    version = tomllib.loads((_SCRIPT.parent.parent / "pyproject.toml").read_text())["project"]["version"]
    assert result.stdout.strip() == version
    args = json.loads((calls.parent / "podman.json").read_text())["build_args"]
    assert args[args.index("--build-arg") + 1] == f"BEEHIVE_REVISION={revision}"
    assert args[args.index("--label") + 1] == f"org.opencontainers.image.revision={revision}"
    assert f"org.opencontainers.image.version={version}" in args
    assert f"{_IMAGE}:{version}" in args
    assert f"{_IMAGE}:{revision[:7]}" in args


def test_promote_keeps_the_previous_image_as_rollback(harness):
    setup, run, tags, _, calls = harness
    setup({"latest": "aaa", "abc1234": "bbb"})

    result = run("promote", "abc1234")

    assert result.returncode == 0, result.stderr
    assert tags() == {"latest": "bbb", "rollback": "aaa", "abc1234": "bbb"}
    log = calls.read_text()
    assert "restart beehive-research.service beehive-jobs.service beehive-web.service" in log
    assert "is-active --quiet beehive-jobs.service" in log


def test_promote_rollback_swaps_back_to_the_previous_image(harness):
    setup, run, tags, _, _ = harness
    setup({"latest": "bad", "rollback": "good"})

    result = run("promote", "rollback")

    assert result.returncode == 0, result.stderr
    assert tags() == {"latest": "good", "rollback": "bad"}


def test_prune_removes_only_unused_release_images(harness):
    setup, run, tags, images, _ = harness
    setup({
        "latest": "new",
        "rollback": "old",
        "5f324c5": "new",
        "bf4553a": "old",
        "1234567": "stale",
        "1.2.0": "new",
        "1.1.0": "old",
        "1.0.0": "stale",
        "civic-projection": "civic",
    })

    result = run("prune")

    assert result.returncode == 0, result.stderr
    assert "1234567" not in tags()
    assert "1.0.0" not in tags()
    assert {
        "latest", "rollback", "5f324c5", "bf4553a", "1.2.0", "1.1.0", "civic-projection",
    } <= set(tags())
    assert "stale" not in images()
    # Images the script did not untag, including untagged ones from other work, stay put.
    assert {"new", "old", "civic", "orphan"} <= set(images())


def test_promote_fails_when_a_worker_keeps_restarting_on_the_new_image(harness):
    setup, run, tags, _, calls = harness
    setup({"latest": "aaa", "abc1234": "bbb"})
    counter = calls.parent / "restarts"
    counter.write_text("0")
    systemctl = calls.parent / "bin" / "systemctl"
    # Every NRestarts query for the jobs worker reports one more restart than the last.
    systemctl.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> "{calls}"\n'
        'case "$*" in *"NRestarts"*"beehive-jobs.service"*)\n'
        f'  n=$(cat "{counter}"); echo $((n + 1)) > "{counter}"; echo "$n";;\n'
        "esac\n"
    )

    result = run("promote", "abc1234")

    assert result.returncode != 0
    assert "beehive-jobs.service restarted on the new image" in result.stderr
