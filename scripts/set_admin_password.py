#!/usr/bin/env python
"""One-time CLI to set (or rotate) the admin password. Run with
`python -m scripts.set_admin_password --db-path /data/beehive.db` (or let --db-path default to
DB_PATH/the standard path like the other scripts). Prompts via getpass (never echoed, never in
shell history or the process list) and stores only the Argon2id hash, in the app's own DB — so
rotating the password later is just re-running this command, no redeploy needed."""
from __future__ import annotations

import argparse
import getpass
import os

from beehive.auth.passwords import hash_password
from beehive.db import app_state
from beehive.db.connection import connect, init_schema

_PASSWORD_HASH_KEY = "admin_password_hash"


def set_admin_password(db_path: str, password: str) -> None:
    conn = connect(db_path)
    try:
        init_schema(conn)
        app_state.set(conn, _PASSWORD_HASH_KEY, hash_password(password))
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default=os.environ.get("DB_PATH", "/data/beehive.db"))
    args = parser.parse_args()

    password = getpass.getpass("New admin password: ")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("Passwords did not match. Nothing was changed.")
        raise SystemExit(1)
    if not password:
        print("Password cannot be empty. Nothing was changed.")
        raise SystemExit(1)

    set_admin_password(args.db_path, password)
    print("Admin password updated.")


if __name__ == "__main__":
    main()
