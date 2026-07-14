#!/usr/bin/env python
"""Entrypoint for the always-on web container (deploy/quadlet/beehive-web.container)."""
from __future__ import annotations

import os

import uvicorn

from beehive.web.app import create_app


def main() -> None:
    db_path = os.environ.get("DB_PATH", "/data/beehive.db")
    app = create_app(db_path)
    # 0.0.0.0: must accept connections arriving on the container's veth interface, not just its
    # own loopback -- Podman's PublishPort=127.0.0.1:8095:8000 already restricts real external
    # access to the host's own loopback, so this doesn't widen exposure. 127.0.0.1 here would
    # silently refuse all port-published traffic.
    uvicorn.run(app, host="0.0.0.0", port=8000)  # noqa: S104


if __name__ == "__main__":
    main()
