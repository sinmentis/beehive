"""Tell a whole-site outage of a Civic source apart from a defect in how we fetch it.

A source is held (reported, not failed) only when its listing and its human-verified health URL
both come back with an HTTP error answer from the origin. A listing that fails while the health
URL answers is ours to fix, and a request that never got an answer (DNS, TLS, timeout, reset)
may be our own network, so both keep failing. A held outage fails again once it has lasted
``OUTAGE_ESCALATION_AFTER``, so an operator decides what to do with the source.
"""
from __future__ import annotations

from datetime import timedelta

from beehive.civic.fetching import CivicEndpoint, CivicTransport, CrawlPacer
from beehive.connectors.civic_strategies import party_media_transport
from beehive.connectors.http import ConnectorHttpError, fetch_bytes

OUTAGE_ESCALATION_AFTER = timedelta(days=7)
MAX_HELD_SOURCE_FRACTION = 0.1
HEALTH_PROBE_MAX_BYTES = 256 * 1024
HEALTH_PROBE_TIMEOUT_SECONDS = 20.0

# Origin answers that mean a site is gone, refusing us, or failing. 429 and other 4xx mean the
# site answered and objects to this request, so they stay failures. Only server and CDN-origin
# failures count among 5xx: 501 and 505-510 describe the request, and 511 comes from a captive
# portal on our side of the network.
_SERVER_FAILURE_STATUSES = frozenset(
    {500, 502, 503, 504, 520, 521, 522, 523, 524, 525, 526, 530})
OUTAGE_STATUSES = frozenset({401, 403, 404, 410}) | _SERVER_FAILURE_STATUSES

# Pages verified to answer our party/media user agent, so their failing is outage evidence.
# Checked 2026-09-28: each answered with HTML except Conservative, whose Wix domain had been
# disconnected; it served its article pages until at least 2026-09-15 and is the party website
# on the Electoral Commission register. Green, NZ First, Te Pati Maori and Opportunity refuse
# page requests (403) while their feeds work, so their homepages prove nothing and are absent.
# A source without an entry keeps failing loudly.
OUTAGE_HEALTH_URLS = {
    "1news": "https://www.1news.co.nz/",
    "act": "https://www.act.org.nz/",
    "alliance": "https://allianceparty.nz/",
    "animal-justice": "https://animaljustice.org.nz/",
    "conservative": "https://www.conservatives.nz/",
    "interest": "https://www.interest.co.nz/",
    "labour": "https://www.labour.org.nz/",
    "national": "https://www.national.org.nz/",
    "newsroom": "https://newsroom.co.nz/",
    "odt": "https://www.odt.co.nz/",
    "outdoors-freedom": "https://nzofp.co.nz/",
    "spinoff": "https://thespinoff.co.nz/",
    "stuff": "https://www.stuff.co.nz/",
    "te-ao": "https://www.teaonews.co.nz/",
    "the-post": "https://www.thepost.co.nz/",
    "vision-nz": "https://www.vision.org.nz/",
    "waatea": "https://waateanews.com/",
    "womens-rights": "https://womensrightsparty.nz/",
}


def origin_status(exc: BaseException) -> int | None:
    """The HTTP status behind a fetch error, or None when no origin answered."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ConnectorHttpError):
            return current.status
        current = current.__cause__ or current.__context__
    return None


def outage_status(exc: BaseException) -> int | None:
    status = origin_status(exc)
    return status if status in OUTAGE_STATUSES else None


def _single_attempt(url: str, **kwargs):
    return fetch_bytes(url, max_attempts=1, **kwargs)


def health_probe_transport(pacer: CrawlPacer) -> CivicTransport:
    """The party/media transport on the cycle's own crawl pacer, making one attempt."""
    return party_media_transport(fetch=_single_attempt, pacer=pacer)


def probe_health_url(entry: object, transport: CivicTransport) -> int | None:
    """Request the source's verified health URL once; its outage status if it failed with one."""
    url = OUTAGE_HEALTH_URLS.get(getattr(entry, "source_key", ""))
    if url is None:
        return None
    endpoint = CivicEndpoint(
        url=url,
        allowed_hosts=frozenset(entry.hosts),
        content_types=frozenset(),
        max_bytes=HEALTH_PROBE_MAX_BYTES,
        timeout_seconds=HEALTH_PROBE_TIMEOUT_SECONDS,
    )
    try:
        transport.request(
            endpoint,
            source_key=entry.source_key,
            crawl_delay_seconds=getattr(
                getattr(entry, "definition", None), "crawl_delay_seconds", 0.0),
        )
    except Exception as exc:  # noqa: BLE001 -- anything but an outage answer is not evidence
        return outage_status(exc)
    return None


def held_source_limit(source_count: int) -> int:
    """More simultaneous outages than this points at our own network, not the sites."""
    return max(1, int(source_count * MAX_HELD_SOURCE_FRACTION))


__all__ = [
    "MAX_HELD_SOURCE_FRACTION",
    "OUTAGE_ESCALATION_AFTER",
    "OUTAGE_HEALTH_URLS",
    "OUTAGE_STATUSES",
    "health_probe_transport",
    "held_source_limit",
    "origin_status",
    "outage_status",
    "probe_health_url",
]
