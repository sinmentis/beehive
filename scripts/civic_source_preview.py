#!/usr/bin/env python
"""Opt-in live preview of one Civic source. The ONLY code path in Civic that touches the network.

Running it without `--live` performs no request at all: it prints what *would* be fetched (URL,
method, host allow-list, crawl delay, assigned tier) and exits. That default is the point --
inspecting the catalog is the common operation, and a tool that hits fifteen government sites by
accident is exactly what a well-behaved crawler must not ship with.

    scripts/civic_source_preview.py --list
    scripts/civic_source_preview.py nz_electoral_commission          # dry run, no network
    scripts/civic_source_preview.py nz_electoral_commission --live   # one real request

`--live` fetches exactly one source, once, honouring its crawl delay, its byte cap and its host
allow-list, and prints the normalized documents plus the fetch health. It never writes to the
database and never runs a Civic Run. Nothing here accepts a URL: the source key selects a fixed
definition from `beehive.civic.catalog`, so there is no argument that can point it somewhere new.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from beehive.civic.catalog import all_sources, enabled_sources, get_source
from beehive.civic.documents import CivicSourceError

_PREVIEW_LIMIT = 5


def _print_catalog() -> None:
    available = {entry.source_key for entry in enabled_sources()}
    print(f"{'source key':34} {'tier':10} {'delay':>6}  endpoint")
    for entry in all_sources():
        marker = " " if entry.source_key in available else "-"
        print(f"{marker}{entry.source_key:33} {entry.tier.value:10} "
              f"{entry.crawl_delay_seconds:>6}  {entry.url}")
    missing = [entry for entry in all_sources() if entry.source_key not in available]
    for entry in missing:
        print(f"\n- {entry.source_key} is unavailable: {entry.requires_api_key_env} is not set "
              "(optional; Civic runs without it)")


def _describe(entry) -> None:
    endpoint = entry.definition.endpoint
    print(f"source key      : {entry.source_key}")
    print(f"publisher       : {entry.publisher}")
    print(f"tier            : {entry.tier.value}   (assigned by application config)")
    print(f"endpoint        : {endpoint.method} {endpoint.url}")
    print(f"allowed hosts   : {sorted(endpoint.allowed_hosts)}")
    print(f"content types   : {sorted(endpoint.content_types)}")
    print(f"max bytes       : {endpoint.max_bytes}")
    print(f"timeout seconds : {endpoint.timeout_seconds}")
    print(f"crawl delay     : {entry.crawl_delay_seconds}s")
    if entry.requires_api_key_env:
        print(f"optional key    : {entry.requires_api_key_env}")


def _preview(entry, limit: int) -> int:
    connector = entry.build()
    now = datetime.now(timezone.utc)
    try:
        result = connector.fetch(now)
    except CivicSourceError as exc:
        print(f"FAILED  {exc}", file=sys.stderr)
        return 1
    print(f"health          : {result.health.value}")
    print(f"pages fetched   : {result.pages_fetched}")
    print(f"documents       : {len(result.documents)}")
    for reason in result.degraded_reasons:
        print(f"  degraded      : {reason}")
    for document in result.documents[:limit]:
        print()
        print(f"  title         : {document.title}")
        print(f"  external key  : {document.external_key}")
        print(f"  canonical url : {document.canonical_url}")
        print(f"  published at  : {document.published_at}")
        print(f"  updated at    : {document.updated_at}")
        print(f"  first seen at : {document.first_seen_at}")
        if document.excerpt:
            print(f"  excerpt       : {document.excerpt[:160]}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source_key", nargs="?", help="a key from --list")
    parser.add_argument("--list", action="store_true", help="show the catalog and exit")
    parser.add_argument(
        "--live", action="store_true",
        help="actually perform one request (off by default: no network without this flag)")
    parser.add_argument("--limit", type=int, default=_PREVIEW_LIMIT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.list or not args.source_key:
        _print_catalog()
        return 0
    try:
        entry = get_source(args.source_key)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    _describe(entry)
    if not args.live:
        print("\ndry run: no request was made. Pass --live to fetch this source once.")
        return 0
    print()
    return _preview(entry, args.limit)


if __name__ == "__main__":
    raise SystemExit(main())
