"""One HTTP client for every connector, so the operational rules a connector must not get wrong
live in exactly one place instead of being re-derived (or forgotten) per provider.

What this centralizes, and why each rule exists:

- **SSRF validation.** Every request target goes through `url_safety.assert_fetchable_url`. This
  matters most for URLs the app did not construct itself: `reddit.fetch_comments` builds its
  target from a `<link href>` that arrived in a feed and was then stored in the database, so
  without this check a `file://`, `http://127.0.0.1:8000/...` or link-local URL was reachable.
  Callers that know the provider pass `allowed_hosts` for a much tighter control.
- **Raw and decompressed response-size caps.** `response.read()` with no argument was previously
  used by all seven urllib connectors, so one oversized or hostile response could exhaust the
  512 MB container (`land_sea_collection`'s module docstring records that this already happened
  once). Reading `cap + 1` raw bytes and applying the same cap while decoding gzip/deflate makes
  both oversized responses and compression bombs clean, per-source errors.
- **Retry with jittered backoff, honouring `Retry-After`.** Previously only
  `shopify_collection` retried, and only on `HTTPError` -- so `URLError`, `socket.timeout`, and
  connection resets, which are the common transients, lost the source for a whole
  `fetch_interval_hours` (up to 24 h). Backoff is jittered because every Source in a Channel
  fetches in the same cycle, and unjittered linear backoff makes them retry in lockstep.
- **Conditional GET.** `fetch_bytes` accepts and returns validators, so a caller that persists
  them can turn an unchanged feed into a 304 with no body. `NotModified` is a distinct outcome
  rather than an exception, because "nothing changed" is a normal, common result.
- **A typed error taxonomy** (`ConnectorHttpError.kind`). `record_fetch_error` previously stored
  whatever `str(exc)` produced, so nothing downstream could tell "transient 429, retry sooner"
  from "this host does not exist, tell the Owner". `deep_read.fetch.FetchFailureReason` is the
  model this follows.

Not a general-purpose HTTP library: it does exactly what the connectors need (GET, bounded body,
no redirect-following beyond urllib's own same-scheme default) and nothing more.
"""
from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from enum import Enum
from typing import Any

from beehive.url_safety import UnsafeUrlError, assert_fetchable_url

DEFAULT_USER_AGENT = "beehive/0.1 (+https://github.com/sinmentis/beehive)"
DEFAULT_TIMEOUT_SECONDS = 30.0
# 8 MiB is comfortably above the largest legitimate response any current connector sees (the
# widest Shopify collection page is ~1 MB of JSON) while staying far below the container's
# 512 MB ceiling even if several Sources fail this way in the same cycle.
DEFAULT_MAX_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_ATTEMPTS = 3
_MAX_BACKOFF_SECONDS = 30.0
# Cap on an upstream-supplied Retry-After: a hostile or misconfigured origin must not be able to
# park a collector process for hours. Past this we give up on the attempt instead of sleeping.
_MAX_HONORED_RETRY_AFTER_SECONDS = 60.0
_RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})
_SUPPORTED_CONTENT_ENCODINGS = frozenset({"", "identity", "gzip", "x-gzip", "deflate"})
_GZIP_WBITS = zlib.MAX_WBITS | 16


class ConnectorHttpErrorKind(str, Enum):
    """Why a connector fetch failed, in terms a caller can branch on.

    TRANSIENT covers anything worth retrying on the normal schedule (5xx, 429, timeouts, resets).
    UNSAFE_URL and NOT_FOUND are permanent for this configuration and mean the Owner (or a
    provider change) needs to act. TOO_LARGE and PROTOCOL mean the provider returned something
    this connector cannot process, which usually signals an upstream format change.
    """

    TRANSIENT = "transient"
    UNSAFE_URL = "unsafe_url"
    NOT_FOUND = "not_found"
    ACCESS_DENIED = "access_denied"
    TOO_LARGE = "too_large"
    PROTOCOL = "protocol"


class ConnectorHttpError(RuntimeError):
    def __init__(self, kind: ConnectorHttpErrorKind, message: str):
        super().__init__(message)
        self.kind = kind

    def __str__(self) -> str:
        return f"[{self.kind.value}] {super().__str__()}"


@dataclass(frozen=True)
class HttpResponse:
    """`body` is None exactly when `not_modified` is True -- a 304 carries no body by
    definition, and the caller is expected to keep whatever it parsed last time."""

    body: bytes | None
    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False


def _classify_http_error(exc: urllib.error.HTTPError) -> ConnectorHttpErrorKind:
    if exc.code in _RETRYABLE_STATUS_CODES:
        return ConnectorHttpErrorKind.TRANSIENT
    if exc.code in (401, 403):
        return ConnectorHttpErrorKind.ACCESS_DENIED
    if exc.code == 404 or exc.code == 410:
        return ConnectorHttpErrorKind.NOT_FOUND
    return ConnectorHttpErrorKind.PROTOCOL


def _retry_after_seconds(exc: urllib.error.HTTPError) -> float | None:
    """The upstream's own Retry-After, in seconds, if it sent a usable one. Supports both the
    delta-seconds and the HTTP-date form."""
    raw = exc.headers.get("Retry-After") if exc.headers else None
    if not raw:
        return None
    raw = raw.strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        target = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if target is None:
        return None
    if target.tzinfo is None:
        return None
    return max(0.0, (target - datetime.now(timezone.utc)).total_seconds())


def _backoff_seconds(attempt: int) -> float:
    """Exponential with full jitter. Jitter matters because every Source in a Channel is fetched
    in the same cycle, so a shared upstream outage would otherwise have them all retry in
    lockstep and hammer the recovering origin."""
    ceiling = min(_MAX_BACKOFF_SECONDS, 2.0 ** attempt)
    return random.uniform(0.0, ceiling)


def _decompress_capped(
    body: bytes,
    *,
    wbits: int,
    max_bytes: int,
    url: str,
) -> bytes:
    decompressor = zlib.decompressobj(wbits)
    decoded = decompressor.decompress(body, max_bytes + 1)
    if len(decoded) > max_bytes:
        raise ConnectorHttpError(
            ConnectorHttpErrorKind.TOO_LARGE,
            f"{url}: decompressed response body exceeds the {max_bytes} byte cap",
        )
    decoded += decompressor.flush(max_bytes - len(decoded) + 1)
    if len(decoded) > max_bytes:
        raise ConnectorHttpError(
            ConnectorHttpErrorKind.TOO_LARGE,
            f"{url}: decompressed response body exceeds the {max_bytes} byte cap",
        )
    if not decompressor.eof or decompressor.unused_data:
        raise zlib.error("compressed response is incomplete or has trailing data")
    return decoded


def _decode_content_encoding(
    body: bytes,
    *,
    encoding: str,
    max_bytes: int,
    url: str,
) -> bytes:
    normalized = encoding.strip().lower()
    if normalized in ("", "identity"):
        return body
    if normalized not in _SUPPORTED_CONTENT_ENCODINGS:
        raise ConnectorHttpError(
            ConnectorHttpErrorKind.PROTOCOL,
            f"{url}: unsupported Content-Encoding {normalized!r}",
        )
    try:
        if normalized in ("gzip", "x-gzip"):
            return _decompress_capped(
                body,
                wbits=_GZIP_WBITS,
                max_bytes=max_bytes,
                url=url,
            )
        try:
            return _decompress_capped(
                body,
                wbits=zlib.MAX_WBITS,
                max_bytes=max_bytes,
                url=url,
            )
        except zlib.error:
            return _decompress_capped(
                body,
                wbits=-zlib.MAX_WBITS,
                max_bytes=max_bytes,
                url=url,
            )
    except ConnectorHttpError:
        raise
    except zlib.error as exc:
        raise ConnectorHttpError(
            ConnectorHttpErrorKind.PROTOCOL,
            f"{url}: invalid {normalized} response body: {exc}",
        ) from exc


def _read_capped(response, max_bytes: int, url: str) -> bytes:
    declared = response.headers.get("Content-Length")
    if declared is not None:
        try:
            if int(declared) > max_bytes:
                raise ConnectorHttpError(
                    ConnectorHttpErrorKind.TOO_LARGE,
                    f"{url}: Content-Length {declared} exceeds the {max_bytes} byte cap")
        except ValueError:
            pass  # a malformed Content-Length is not itself fatal; the read cap still applies
    body = response.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise ConnectorHttpError(
            ConnectorHttpErrorKind.TOO_LARGE,
            f"{url}: response body exceeds the {max_bytes} byte cap")
    return _decode_content_encoding(
        body,
        encoding=response.headers.get("Content-Encoding") or "identity",
        max_bytes=max_bytes,
        url=url,
    )


def fetch_bytes(
    url: str,
    *,
    allowed_hosts: frozenset[str] | None = None,
    user_agent: str = DEFAULT_USER_AGENT,
    accept: str | None = None,
    extra_headers: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    etag: str | None = None,
    last_modified: str | None = None,
    sleep=None,
) -> HttpResponse:
    """GETs `url` and returns its bounded body plus any cache validators it carried.

    Raises `ConnectorHttpError` (never a bare urllib exception) so `record_fetch_error` stores a
    classified failure. Passing `etag`/`last_modified` from a previous fetch turns an unchanged
    resource into `HttpResponse(body=None, not_modified=True)` with no body transferred.
    """
    try:
        assert_fetchable_url(url, allowed_hosts=allowed_hosts)
    except UnsafeUrlError as exc:
        raise ConnectorHttpError(ConnectorHttpErrorKind.UNSAFE_URL, str(exc)) from exc

    headers = {
        "User-Agent": user_agent,
        # Pinned explicitly rather than left to the client: httpx/urllib derive this from what
        # happens to be installed, so a transitive dependency adding a brotli or zstd codec would
        # silently start advertising an encoding the parsing side does not handle.
        "Accept-Encoding": "gzip, deflate",
    }
    if accept is not None:
        headers["Accept"] = accept
    if extra_headers:
        headers.update(extra_headers)
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    last_error: ConnectorHttpError | None = None
    # Resolved per call rather than bound as a default, so `time.sleep` stays patchable.
    sleep = sleep or time.sleep
    for attempt in range(max_attempts):
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                return HttpResponse(
                    body=_read_capped(response, max_bytes, url),
                    etag=response.headers.get("ETag"),
                    last_modified=response.headers.get("Last-Modified"),
                )
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                return HttpResponse(
                    body=None, etag=etag, last_modified=last_modified, not_modified=True)
            kind = _classify_http_error(exc)
            last_error = ConnectorHttpError(kind, f"{url}: HTTP {exc.code} {exc.reason}")
            if kind is not ConnectorHttpErrorKind.TRANSIENT:
                raise last_error from exc
            honored = _retry_after_seconds(exc)
            if honored is not None and honored > _MAX_HONORED_RETRY_AFTER_SECONDS:
                raise last_error from exc
            delay = honored if honored is not None else _backoff_seconds(attempt)
        except ConnectorHttpError:
            raise
        except (urllib.error.URLError, OSError) as exc:
            last_error = ConnectorHttpError(
                ConnectorHttpErrorKind.TRANSIENT, f"{url}: {type(exc).__name__}: {exc}")
            delay = _backoff_seconds(attempt)

        if attempt == max_attempts - 1:
            break
        sleep(delay)

    assert last_error is not None
    raise last_error


def fetch_text(url: str, **kwargs) -> str:
    """`fetch_bytes` decoded as text. Decoding is deliberately lenient (`errors="replace"`): a
    single bad byte in one product description must not lose a whole page of a storefront."""
    response = fetch_bytes(url, **kwargs)
    if response.body is None:
        raise ConnectorHttpError(
            ConnectorHttpErrorKind.PROTOCOL,
            f"{url}: 304 Not Modified returned to a caller that did not ask for text")
    return response.body.decode("utf-8", errors="replace")


def fetch_json(url: str, **kwargs) -> Any:
    """`fetch_bytes` parsed as JSON, with a decode failure reported as PROTOCOL rather than as a
    bare `json.JSONDecodeError`. An upstream that starts serving an HTML error page where JSON
    used to be is the common cause, and the caller should treat that as "provider changed", not
    as a bug in its own parsing."""
    body = fetch_text(url, **kwargs)
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise ConnectorHttpError(
            ConnectorHttpErrorKind.PROTOCOL, f"{url}: response is not valid JSON: {exc}") from exc
