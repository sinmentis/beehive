"""Loader for the small civic connector fixtures.

Each fixture is a trimmed copy of one real document shape (a NationBuilder RSS feed, an Arc Atom
feed, a Google news sitemap, a Wix sitemap index, an embedded publication list, a listing page),
kept just large enough to exercise the rule under test. `act_sitemap_en_nz_live_shape.xml` and
`odt_sitemap_news.xml` preserve the real documents' ordering, because what they are testing is
that releases are still reachable past a hundred static pages.

No test in this package touches the network: fetches go through the canonical
`beehive.civic.fetching.CivicTransport` with `tests.civic.civic_stubs.transport_for`, and these
bytes are what it returns.
"""
from __future__ import annotations

from pathlib import Path

_FIXTURE_DIR = Path(__file__).parent / "civic_fixtures"


def civic_fixture(name: str) -> bytes:
    return (_FIXTURE_DIR / name).read_bytes()
