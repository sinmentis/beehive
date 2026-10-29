"""Validate the numeric package version against published release tags."""
from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

_VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


def _parts(value: str) -> tuple[int, ...] | None:
    match = _VERSION.fullmatch(value)
    return tuple(map(int, match.groups())) if match else None


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], text=True, stderr=subprocess.PIPE,
    ).strip()


def release_version(repo: Path) -> str:
    with (repo / "pyproject.toml").open("rb") as handle:
        version = tomllib.load(handle)["project"]["version"]
    current = _parts(version) if isinstance(version, str) else None
    if current is None:
        raise ValueError("project.version must be numeric MAJOR.MINOR.PATCH")

    published = {}
    for tag in _git(repo, "tag", "--list", "v*").splitlines():
        parts = _parts(tag[1:])
        if parts is not None:
            published[tag] = parts

    tag = f"v{version}"
    if tag in published:
        if _git(repo, "rev-parse", f"refs/tags/{tag}^{{commit}}") == _git(repo, "rev-parse", "HEAD"):
            return version
        raise ValueError(f"{tag} is already released; bump the version before another release")

    if published:
        latest = max(published, key=published.get)
        if current <= published[latest]:
            raise ValueError(f"Version {version} must be newer than the latest release {latest}")
    return version


if __name__ == "__main__":
    try:
        print(release_version(Path(__file__).resolve().parents[1]))
    except (KeyError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"release: {exc}", file=sys.stderr)
        sys.exit(1)
