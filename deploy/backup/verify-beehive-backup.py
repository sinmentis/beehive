#!/usr/bin/env python3
"""Restore drill for one Beehive backup archive: decompress it into a scratch file and prove it
is a usable database (integrity check, core tables present, rows readable). Prints one summary
line and exits non-zero on any problem. Standard library only, so it runs on the bare host."""
from __future__ import annotations

import argparse
import gzip
import os
import shutil
import sqlite3
import sys
import tempfile

CORE_TABLES = ("app_state", "channels", "sources", "items", "item_events", "email_groups")


def verify(archive: str, items_between: tuple[int, int] | None) -> str:
    with tempfile.TemporaryDirectory() as scratch:
        restored = os.path.join(scratch, "restored.db")
        with gzip.open(archive, "rb") as source, open(restored, "wb") as target:
            shutil.copyfileobj(source, target)
        conn = sqlite3.connect(f"file:{restored}?mode=ro", uri=True)
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise ValueError(f"integrity_check failed: {integrity}")
            tables = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            missing = [table for table in CORE_TABLES if table not in tables]
            if missing:
                raise ValueError(f"missing core tables: {', '.join(missing)}")
            counts = {
                table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("channels", "sources", "items", "item_events")
            }
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()
    if items_between is not None:
        low, high = items_between
        if not low <= counts["items"] <= high:
            raise ValueError(
                f"backup holds {counts['items']} items, outside the live range {low}..{high}")
    summary = ", ".join(f"{table}={count}" for table, count in counts.items())
    return f"verified {os.path.basename(archive)}: integrity ok, schema v{version}, {summary}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive")
    parser.add_argument("--items-between", nargs=2, type=int, metavar=("LOW", "HIGH"))
    args = parser.parse_args(argv)
    try:
        print(verify(args.archive, tuple(args.items_between) if args.items_between else None))
    except (OSError, sqlite3.Error, ValueError, EOFError) as exc:
        print(f"verify-beehive-backup: {args.archive}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
