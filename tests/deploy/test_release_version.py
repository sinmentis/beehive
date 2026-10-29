"""Numeric release versions advance, while published versions stay tied to one commit."""
from __future__ import annotations

import subprocess

import pytest

from scripts.release_version import release_version


@pytest.fixture
def repository(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.check_output([
            "git", "-C", str(repo),
            "-c", "user.name=Release Test", "-c", "user.email=test@example.invalid",
            "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false",
            "-c", "core.hooksPath=/dev/null",
            "-c", "init.defaultBranch=main", *args,
        ], text=True).strip()

    def commit(version):
        (repo / "pyproject.toml").write_text(f'[project]\nversion = "{version}"\n')
        git("add", "pyproject.toml")
        git("commit", "--quiet", "--allow-empty", "-m", "test: release fixture")

    git("init", "--quiet")
    commit("1.0.0")
    return repo, git, commit


def test_first_numeric_release_needs_no_existing_tag(repository):
    repo, git, _ = repository
    git("tag", "civic-abc1234")
    assert release_version(repo) == "1.0.0"


@pytest.mark.parametrize("annotated", [False, True])
def test_exact_tagged_commit_can_be_rebuilt(repository, annotated):
    repo, git, commit = repository
    if annotated:
        git("tag", "-a", "v1.0.0", "-m", "v1.0.0")
    else:
        git("tag", "v1.0.0")
    commit("2.0.0")
    git("tag", "v2.0.0")
    git("checkout", "--quiet", "--detach", "v1.0.0")

    assert release_version(repo) == "1.0.0"


def test_new_commit_cannot_reuse_a_published_version(repository):
    repo, git, commit = repository
    git("tag", "v1.0.0")
    commit("1.0.0")

    with pytest.raises(ValueError, match="already released"):
        release_version(repo)


def test_bumped_version_can_be_released(repository):
    repo, git, commit = repository
    git("tag", "v1.0.0")
    commit("1.0.1")

    assert release_version(repo) == "1.0.1"


def test_untagged_version_cannot_go_backwards(repository):
    repo, git, commit = repository
    commit("2.0.0")
    git("tag", "v2.0.0")
    commit("1.5.0")

    with pytest.raises(ValueError, match="newer than"):
        release_version(repo)


@pytest.mark.parametrize("version", ["abc1234", "1.0", "01.0.0"])
def test_releases_require_three_numeric_components(repository, version):
    repo, _, commit = repository
    commit(version)

    with pytest.raises(ValueError, match="MAJOR.MINOR.PATCH"):
        release_version(repo)
