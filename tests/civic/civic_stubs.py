"""Shared offline test doubles for the Civic connectors.

`RecordedFetch` replaces `beehive.connectors.http.fetch_bytes` with a lookup over recorded
fixtures. It deliberately keeps the real signature -- including `allowed_hosts`, `max_bytes`,
`method` and `body` -- and records every call, so a test can assert what a connector *actually
asked for* (host allow-list, POST payload, user agent) rather than only what it returned.

No test in this package touches the network. `scripts/civic_source_preview.py` is the only path
that can, and it requires an explicit opt-in flag.
"""
from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field
from typing import Any

from beehive.civic.fetching import CrawlPacer, CivicTransport
from beehive.connectors.http import ConnectorHttpError, ConnectorHttpErrorKind, HttpResponse

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def fixture_json(name: str) -> Any:
    return json.loads(fixture_bytes(name))


@dataclass
class RecordedCall:
    url: str
    allowed_hosts: frozenset[str] | None
    user_agent: str
    method: str
    body: bytes | None
    max_bytes: int
    extra_headers: dict[str, str] | None
    accept: str | None

    @property
    def payload(self) -> Any:
        return json.loads(self.body) if self.body else None


class RecordedFetch:
    """A `fetch_bytes` stand-in backed by fixtures, keyed by URL.

    `routes` maps a URL to either bytes, an `(bytes, content_type)` pair, or an exception to
    raise. A URL with no route raises AssertionError rather than silently returning empty, so a
    connector that starts fetching somewhere unexpected fails loudly."""

    def __init__(self, routes: dict[str, Any], *, default_content_type: str = "application/xml"):
        self._routes = routes
        self._default_content_type = default_content_type
        self.calls: list[RecordedCall] = []

    def __call__(
        self,
        url: str,
        *,
        allowed_hosts=None,
        user_agent="",
        accept=None,
        extra_headers=None,
        timeout=None,
        max_bytes=0,
        method="GET",
        body=None,
        **_unused,
    ) -> HttpResponse:
        self.calls.append(RecordedCall(
            url=url, allowed_hosts=allowed_hosts, user_agent=user_agent, method=method,
            body=body, max_bytes=max_bytes, extra_headers=extra_headers, accept=accept))
        try:
            route = self._routes[url]
        except KeyError:
            raise AssertionError(
                f"connector fetched an unrouted URL: {url}\nrouted: {sorted(self._routes)}"
            ) from None
        if isinstance(route, BaseException):
            raise route
        if isinstance(route, tuple):
            payload, content_type = route
        else:
            payload, content_type = route, self._default_content_type
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        return HttpResponse(body=payload, content_type=content_type)

    @property
    def urls(self) -> list[str]:
        return [call.url for call in self.calls]


@dataclass
class FakeClock:
    """A monotonic clock the pacer can be tested against without ever sleeping. `sleep` advances
    the clock exactly as a real sleep would, so pacing arithmetic is observable."""

    now: float = 0.0
    slept: list[float] = field(default_factory=list)

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


def transport_for(routes: dict[str, Any], *, clock: FakeClock | None = None,
                  default_content_type: str = "application/xml") -> CivicTransport:
    fetch = RecordedFetch(routes, default_content_type=default_content_type)
    clock = clock or FakeClock()
    pacer = CrawlPacer(clock=clock, sleeper=clock.sleep)
    transport = CivicTransport(fetch=fetch, pacer=pacer)
    transport.recorded = fetch  # type: ignore[attr-defined]
    transport.clock = clock  # type: ignore[attr-defined]
    return transport


class SequencedFetch(RecordedFetch):
    """Answers ANY url with the next body in a sequence.

    For the paging connectors, whose successive requests differ only by a query parameter or a
    POST body: routing by URL would mean spelling out every generated URL in the test, which
    tests the test rather than the connector. `body_for` receives the zero-based call index, so a
    test can return a full page, a full page, then a short one."""

    def __init__(self, body_for, *, content_type: str = "application/json"):
        super().__init__({}, default_content_type=content_type)
        self._body_for = body_for
        self._content_type = content_type

    def __call__(self, url: str, **kwargs) -> HttpResponse:
        index = len(self.calls)
        self.calls.append(RecordedCall(
            url=url, allowed_hosts=kwargs.get("allowed_hosts"),
            user_agent=kwargs.get("user_agent", ""), method=kwargs.get("method", "GET"),
            body=kwargs.get("body"), max_bytes=kwargs.get("max_bytes", 0),
            extra_headers=kwargs.get("extra_headers"), accept=kwargs.get("accept")))
        payload = self._body_for(index) if callable(self._body_for) else self._body_for
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        return HttpResponse(body=payload, content_type=self._content_type)


def sequenced_transport(body_for, *, content_type: str = "application/json") -> CivicTransport:
    clock = FakeClock()
    fetch = SequencedFetch(body_for, content_type=content_type)
    transport = CivicTransport(fetch=fetch, pacer=CrawlPacer(clock=clock, sleeper=clock.sleep))
    transport.recorded = fetch  # type: ignore[attr-defined]
    transport.clock = clock  # type: ignore[attr-defined]
    return transport


def http_error(kind: ConnectorHttpErrorKind, message: str = "boom") -> ConnectorHttpError:
    return ConnectorHttpError(kind, message)
