"""Drives deploy/release.sh against a fake podman that keeps image tags in a JSON file, so tag
moves during promote and prune are checked without touching real images or units."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
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

    if args[:2] == ["image", "exists"]:
        sys.exit(0 if resolve(args[2]) else 1)
    if args[:2] == ["image", "inspect"]:
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


def test_promote_keeps_the_previous_image_as_rollback(harness):
    setup, run, tags, _, calls = harness
    setup({"latest": "aaa", "abc1234": "bbb"})

    result = run("promote", "abc1234")

    assert result.returncode == 0, result.stderr
    assert tags() == {"latest": "bbb", "rollback": "aaa", "abc1234": "bbb"}
    assert "restart beehive-research.service beehive-web.service" in calls.read_text()


def test_promote_rollback_swaps_back_to_the_previous_image(harness):
    setup, run, tags, _, _ = harness
    setup({"latest": "bad", "rollback": "good"})

    result = run("promote", "rollback")

    assert result.returncode == 0, result.stderr
    assert tags() == {"latest": "good", "rollback": "bad"}


def test_prune_removes_only_unused_sha_tagged_images(harness):
    setup, run, tags, images, _ = harness
    setup({
        "latest": "new",
        "rollback": "old",
        "5f324c5": "new",
        "bf4553a": "old",
        "1234567": "stale",
        "civic-projection": "civic",
    })

    result = run("prune")

    assert result.returncode == 0, result.stderr
    assert "1234567" not in tags()
    assert {"latest", "rollback", "5f324c5", "bf4553a", "civic-projection"} <= set(tags())
    assert "stale" not in images()
    # Images the script did not untag, including untagged ones from other work, stay put.
    assert {"new", "old", "civic", "orphan"} <= set(images())
