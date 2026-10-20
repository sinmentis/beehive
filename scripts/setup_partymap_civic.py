#!/usr/bin/env python
"""Plan or apply the single global PartyMap Civic Watch and alias vocabulary."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

from beehive.civic.setup import (
    PartyMapSetupConflictError,
    apply_partymap_setup,
    plan_partymap_setup,
)
from beehive.db.connection import connect, init_schema


def run_setup(
    db_path: str,
    *,
    apply: bool,
    now: datetime,
) -> dict:
    conn = connect(db_path)
    init_schema(conn)
    try:
        if apply:
            result = apply_partymap_setup(conn, now)
            plan = result.plan
            return {
                "mode": "apply",
                "disposition": plan.disposition.value,
                "applied": result.applied,
                "watch_id": result.watch_id,
                "vocabulary_version_id": result.vocabulary_version_id,
                "term_count": plan.term_count,
                "actions": list(plan.actions),
            }
        plan = plan_partymap_setup(conn)
        return {
            "mode": "plan",
            "disposition": plan.disposition.value,
            "applied": False,
            "watch_id": plan.watch_id,
            "vocabulary_version_id": None,
            "term_count": plan.term_count,
            "actions": list(plan.actions),
        }
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db-path", default=os.environ.get("DB_PATH", "/data/beehive.db"))
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the displayed idempotent setup. Without this flag, only plan.",
    )
    args = parser.parse_args(argv)
    try:
        output = run_setup(
            args.db_path,
            apply=args.apply,
            now=datetime.now(timezone.utc),
        )
    except PartyMapSetupConflictError as exc:
        print(
            json.dumps({
                "mode": "apply" if args.apply else "plan",
                "status": "conflict",
                "error": str(exc),
            }, sort_keys=True),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(output, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
