"""Runs the real deploy/backup scripts against a throwaway database: an archive appears only after
the restore drill passes, retention keeps the newest archives, and a corrupt archive fails."""
from __future__ import annotations

import gzip
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from beehive.db.channels import create_channel
from beehive.db.connection import connect, init_schema
from beehive.db.sources import create_source

_BACKUP_DIR = Path(__file__).resolve().parents[2] / "deploy" / "backup"
_SCRIPT = _BACKUP_DIR / "beehive-backup.sh"
_VERIFY = _BACKUP_DIR / "verify-beehive-backup.py"

pytestmark = pytest.mark.skipif(
    shutil.which("sqlite3") is None or shutil.which("bash") is None,
    reason="needs the sqlite3 CLI and bash",
)


def _database(tmp_path: Path) -> Path:
    path = tmp_path / "beehive.db"
    conn = connect(str(path))
    init_schema(conn)
    channel_id = create_channel(conn, "News", "profile")
    source_id = create_source(conn, channel_id, "reddit_subreddit", {"subreddit": "x"})
    conn.execute(
        "INSERT INTO items (source_id, external_id, title, url) VALUES (?, 'a', 'A', 'https://x')",
        (source_id,),
    )
    conn.commit()
    conn.close()
    return path


def _run(db: Path, backup_dir: Path, **extra_env: str) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "BEEHIVE_DB_PATH": str(db),
        "BEEHIVE_BACKUP_DIR": str(backup_dir),
        "BEEHIVE_BACKUP_VERIFY_SCRIPT": str(_VERIFY),
        **extra_env,
    }
    return subprocess.run(
        ["bash", str(_SCRIPT)], env=env, capture_output=True, text=True, check=False)


def test_backup_writes_a_verified_archive(tmp_path):
    backup_dir = tmp_path / "backups"

    result = _run(_database(tmp_path), backup_dir)

    assert result.returncode == 0, result.stderr
    archives = sorted(backup_dir.glob("beehive-*.db.gz"))
    assert len(archives) == 1
    assert "integrity ok" in result.stdout
    assert "items=1" in result.stdout
    assert not list(backup_dir.glob(".*partial"))


def test_retention_drops_old_archives_but_keeps_the_newest(tmp_path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    month_ago = time.time() - 30 * 86400
    for day in range(1, 6):
        old = backup_dir / f"beehive-202608{day:02d}-030000.db.gz"
        old.write_bytes(gzip.compress(b"old"))
        os.utime(old, (month_ago + day, month_ago + day))

    result = _run(_database(tmp_path), backup_dir, BEEHIVE_BACKUP_KEEP_MIN="3")

    assert result.returncode == 0, result.stderr
    remaining = sorted(path.name for path in backup_dir.glob("beehive-*.db.gz"))
    assert len(remaining) == 3
    assert "beehive-20260805-030000.db.gz" in remaining
    assert "beehive-20260804-030000.db.gz" in remaining


def test_a_corrupt_archive_fails_the_restore_drill(tmp_path):
    archive = tmp_path / "beehive-broken.db.gz"
    archive.write_bytes(gzip.compress(b"this is not a database"))

    result = subprocess.run(
        [sys.executable, str(_VERIFY), str(archive)], capture_output=True, text=True,
        check=False)

    assert result.returncode == 1
    assert "verify-beehive-backup" in result.stderr
