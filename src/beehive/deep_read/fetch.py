"""SSRF-safe fetch of a single trusted, already-stored article URL. This is the only place
in deep_read that touches the network -- extract.py never does.

Threat model: the URL comes from a stored source (an Item.url the collector already
accepted), not from a live user request, but it still points at an attacker-influenced
publisher-controlled string, so it is treated as fully untrusted for the purposes of this
fetch. The design follows defense in depth:

1. Structural validation rejects non-http(s) schemes, embedded userinfo credentials, and any
   port other than the scheme's default (80/443) before any I/O happens.
2. The hostname is resolved to *every* address it maps to (A and AAAA); if *any* resolved
   address is loopback/private/link-local/multicast/reserved/unspecified, the whole host is
   rejected -- a hostname that resolves to one public and one private address is exactly the
   "DNS rebinding" / "multi-homed" trick this blocks.
3. The connection is pinned to one validated IP literal: the outgoing TCP connection target
   is that IP, while the `Host` header and TLS SNI/certificate-hostname stay the original
   hostname (via httpx's `sni_hostname` request extension). This closes the TOCTOU gap
   between "resolve and check" and "connect" -- there is no second DNS lookup for an
   attacker-controlled resolver to answer differently. Where the transport exposes the
   underlying socket, `_verify_peer` cross-checks the actual peer address against the pinned
   IP as a second, independent layer.
4. Every redirect response is followed manually (`follow_redirects=False` on the client) --
   the `Location` target goes back through step 1-3 from scratch, so a redirect to a private
   address is rejected exactly like a direct request would be.
5. Redirect count, total wall-clock time, raw response bytes, and *decompressed* bytes are
   all capped, the last one specifically to bound zip-bomb-style compression ratios rather
   than trusting `Content-Length`.
6. Only HTML-compatible content types are accepted, checked from the response headers before
   any body is read.
7. Every failure is a typed `FetchFailure(reason, detail)` -- callers branch on
   `FetchFailureReason`, never on parsing exception strings.

The default transport is real httpx/network I/O; both DNS resolution and the httpx
transport are constructor parameters so tests exercise this whole pipeline (validation,
pinning, redirects, caps, content-type) against a fake resolver and an `httpx.MockTransport`
without any network access. Environment proxies are always disabled (`trust_env=False`)
regardless of what the test or deployment environment sets."""
from __future__ import annotations

import ipaddress
import signal
import socket
import threading
import time
import zlib
from contextlib import contextmanager
from dataclasses import dataclass
from email.message import Message
from enum import Enum
from urllib.parse import urljoin, urlsplit

import httpx

_ALLOWED_SCHEMES = ("http", "https")
_DEFAULT_PORTS = {"http": 80, "https": 443}
_ALLOWED_PORTS = frozenset(_DEFAULT_PORTS.values())
_ALLOWED_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})
_SUPPORTED_CONTENT_ENCODINGS = frozenset({"", "identity", "gzip", "x-gzip", "deflate"})

_DEFAULT_MAX_REDIRECTS = 5
_DEFAULT_CONNECT_TIMEOUT = 5.0
_DEFAULT_TOTAL_TIMEOUT = 20.0
_DEFAULT_MAX_RESPONSE_BYTES = 10 * 1024 * 1024  # raw, on-the-wire bytes
_DEFAULT_MAX_DECOMPRESSED_BYTES = 20 * 1024 * 1024  # after content-encoding is undone
_DEFAULT_USER_AGENT = "beehive-deep-read/0.1 (+https://github.com/sinmentis/beehive)"

# zlib's decompressobj(wbits) magic constant for transparent gzip-container decoding.
_GZIP_WBITS = zlib.MAX_WBITS | 16


class FetchFailureReason(str, Enum):
    MALFORMED_URL = "malformed_url"
    INVALID_SCHEME = "invalid_scheme"
    CREDENTIALS_IN_URL = "credentials_in_url"
    INVALID_PORT = "invalid_port"
    DNS_RESOLUTION_FAILED = "dns_resolution_failed"
    PROHIBITED_ADDRESS = "prohibited_address"
    CONNECTION_FAILED = "connection_failed"
    TLS_ERROR = "tls_error"
    PEER_MISMATCH = "peer_mismatch"
    TOO_MANY_REDIRECTS = "too_many_redirects"
    REDIRECT_MISSING_LOCATION = "redirect_missing_location"
    TIMEOUT = "timeout"
    RESPONSE_TOO_LARGE = "response_too_large"
    DECOMPRESSED_TOO_LARGE = "decompressed_too_large"
    UNSUPPORTED_CONTENT_TYPE = "unsupported_content_type"
    UNSUPPORTED_CONTENT_ENCODING = "unsupported_content_encoding"
    HTTP_ERROR = "http_error"


@dataclass(frozen=True)
class FetchFailure:
    reason: FetchFailureReason
    detail: str
    status_code: int | None = None


@dataclass(frozen=True)
class FetchedArticle:
    url: str  # final URL after any validated redirects
    status_code: int
    content_type: str  # media type only, parameters (e.g. charset) stripped
    html: str
    truncated: bool  # True if a byte/decompressed cap cut the body short


FetchOutcome = FetchedArticle | FetchFailure


class DnsResolutionError(Exception):
    pass


class _AbsoluteFetchTimeout(Exception):
    pass


@contextmanager
def _absolute_fetch_timeout(seconds: float):
    """Interrupt blocking DNS/header/body I/O when running on the worker's main thread."""
    if seconds <= 0:
        raise _AbsoluteFetchTimeout
    if (threading.current_thread() is not threading.main_thread()
            or not hasattr(signal, "setitimer")):
        yield
        return

    previous_delay, previous_interval = signal.getitimer(signal.ITIMER_REAL)
    if previous_delay > 0 or previous_interval > 0:
        yield
        return

    previous_handler = signal.getsignal(signal.SIGALRM)
    if previous_handler is None:
        previous_handler = signal.SIG_DFL

    def raise_timeout(_signum, _frame):
        raise _AbsoluteFetchTimeout

    signal.signal(signal.SIGALRM, raise_timeout)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


def _default_resolve_host(hostname: str) -> list[str]:
    """Returns every unique IP literal (v4 and v6) the resolver has for hostname, or raises
    DnsResolutionError. A bare IP literal in the URL resolves to itself, no lookup needed."""
    try:
        ipaddress.ip_address(hostname)
        return [hostname]
    except ValueError:
        pass

    try:
        infos = socket.getaddrinfo(hostname, None, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise DnsResolutionError(f"could not resolve {hostname!r}: {exc}") from exc

    addresses: list[str] = []
    seen: set[str] = set()
    for _family, _socktype, _proto, _canonname, sockaddr in infos:
        ip = sockaddr[0]
        if ip not in seen:
            seen.add(ip)
            addresses.append(ip)
    if not addresses:
        raise DnsResolutionError(f"resolver returned no addresses for {hostname!r}")
    return addresses


def _is_prohibited_address(ip_literal: str) -> bool:
    """Only globally routable addresses are eligible connection targets.

    This rejects private and special-purpose ranges such as RFC 6598 shared address space even
    when Python does not classify them as ``private`` or ``reserved``. IPv4-mapped IPv6 addresses
    are also checked through their embedded IPv4 address.
    """
    addr = ipaddress.ip_address(ip_literal)
    candidates = [addr]
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        candidates.append(addr.ipv4_mapped)
    return any(
        not c.is_global
        or c.is_loopback
        or c.is_private
        or c.is_link_local
        or c.is_multicast
        or c.is_reserved
        or c.is_unspecified
        for c in candidates)


@dataclass(frozen=True)
class _Endpoint:
    ip: str
    port: int
    scheme: str
    host_header: str
    sni_hostname: str
    path_and_query: str


def _validate_and_resolve(url: str, resolve_host) -> _Endpoint | FetchFailure:
    parts = urlsplit(url)

    if not parts.scheme or not parts.hostname:
        return FetchFailure(FetchFailureReason.MALFORMED_URL, f"not an absolute URL: {url!r}")
    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        return FetchFailure(FetchFailureReason.INVALID_SCHEME, f"scheme {parts.scheme!r} is not http/https")
    if parts.username is not None or parts.password is not None:
        return FetchFailure(FetchFailureReason.CREDENTIALS_IN_URL, "URL contains embedded userinfo credentials")

    port = parts.port if parts.port is not None else _DEFAULT_PORTS[scheme]
    if port not in _ALLOWED_PORTS:
        return FetchFailure(FetchFailureReason.INVALID_PORT, f"port {port} is not 80 or 443")

    hostname = parts.hostname
    try:
        addresses = resolve_host(hostname)
    except DnsResolutionError as exc:
        return FetchFailure(FetchFailureReason.DNS_RESOLUTION_FAILED, str(exc))

    prohibited = [ip for ip in addresses if _is_prohibited_address(ip)]
    if prohibited:
        return FetchFailure(
            FetchFailureReason.PROHIBITED_ADDRESS,
            f"{hostname!r} resolves to a prohibited address ({prohibited[0]}); "
            f"rejecting the whole host ({len(addresses)} address(es) total)",
        )

    path_and_query = parts.path or "/"
    if parts.query:
        path_and_query = f"{path_and_query}?{parts.query}"

    # Deterministic pin target: first address the resolver returned (first success from
    # _default_resolve_host's getaddrinfo order, or the sole literal for an IP-literal host).
    return _Endpoint(
        ip=addresses[0], port=port, scheme=scheme,
        host_header=hostname, sni_hostname=hostname, path_and_query=path_and_query,
    )


def _content_type_and_charset(headers: httpx.Headers) -> tuple[str, str]:
    raw = headers.get("content-type", "")
    message = Message()
    message["content-type"] = raw
    media_type = (message.get_content_type() or "").lower()
    charset = message.get_content_charset() or "utf-8"
    return media_type, charset


def _verify_peer(response: httpx.Response, expected_ip: str) -> bool:
    """Best-effort second layer on top of IP pinning: if the transport exposes the raw
    network stream, confirm the socket actually peered with the pinned IP. Returns True when
    the peer matches OR when it cannot be determined (e.g. under a mock transport in tests,
    or a stream type that doesn't expose peer info) -- pinning the request URL to the
    validated IP is the primary guarantee; this only catches a transport silently connecting
    elsewhere (e.g. via a leaked proxy)."""
    stream = response.extensions.get("network_stream")
    if stream is None:
        return True
    try:
        peer = stream.get_extra_info("peername")
    except Exception:
        return True
    if not peer:
        return True
    try:
        peer_ip = ipaddress.ip_address(peer[0])
        pinned_ip = ipaddress.ip_address(expected_ip)
    except ValueError:
        return True
    return peer_ip == pinned_ip


@dataclass(frozen=True)
class _RawBody:
    text: str
    truncated: bool


def _read_capped_body(
    response: httpx.Response, charset: str, max_response_bytes: int, max_decompressed_bytes: int,
    deadline: float,
) -> _RawBody | FetchFailure:
    encoding = (response.headers.get("content-encoding") or "identity").strip().lower()
    if encoding not in _SUPPORTED_CONTENT_ENCODINGS:
        return FetchFailure(
            FetchFailureReason.UNSUPPORTED_CONTENT_ENCODING, f"unsupported content-encoding {encoding!r}")

    decompressor = None
    if encoding in ("gzip", "x-gzip"):
        decompressor = zlib.decompressobj(_GZIP_WBITS)
    elif encoding == "deflate":
        decompressor = zlib.decompressobj()

    raw_total = 0
    out_chunks: list[bytes] = []
    out_total = 0
    truncated = False

    try:
        for chunk in response.iter_raw():
            if time.monotonic() >= deadline:
                return FetchFailure(
                    FetchFailureReason.TIMEOUT, "overall fetch time budget exceeded while reading")
            raw_total += len(chunk)
            if raw_total > max_response_bytes:
                truncated = True
                break
            if decompressor is not None:
                try:
                    piece = decompressor.decompress(chunk, max(max_decompressed_bytes - out_total, 0) + 1)
                except zlib.error as exc:
                    return FetchFailure(FetchFailureReason.CONNECTION_FAILED, f"decompression failed: {exc}")
            else:
                piece = chunk
            out_chunks.append(piece)
            out_total += len(piece)
            if out_total > max_decompressed_bytes:
                truncated = True
                break

        if not truncated and time.monotonic() >= deadline:
            return FetchFailure(
                FetchFailureReason.TIMEOUT, "overall fetch time budget exceeded while reading")

        if not truncated and decompressor is not None:
            try:
                tail = decompressor.flush()
            except zlib.error as exc:
                return FetchFailure(FetchFailureReason.CONNECTION_FAILED, f"decompression failed: {exc}")
            out_chunks.append(tail)
            out_total += len(tail)
            if out_total > max_decompressed_bytes:
                truncated = True
    finally:
        response.close()

    body = b"".join(out_chunks)
    if truncated and out_total > max_decompressed_bytes:
        body = body[:max_decompressed_bytes]

    try:
        text = body.decode(charset, errors="replace")
    except LookupError:
        text = body.decode("utf-8", errors="replace")
    return _RawBody(text=text, truncated=truncated)


class ArticleFetcher:
    """Injectable, SSRF-safe fetcher for one trusted stored URL. `resolve_host` and
    `transport` are the two test seams: inject a fake resolver to simulate DNS
    (including mixed safe/prohibited answers or rebinding-style sequences) and an
    `httpx.MockTransport` to simulate origin/redirect responses, all without touching a
    real network."""

    def __init__(
        self,
        *,
        resolve_host=_default_resolve_host,
        transport: httpx.BaseTransport | None = None,
        max_redirects: int = _DEFAULT_MAX_REDIRECTS,
        connect_timeout: float = _DEFAULT_CONNECT_TIMEOUT,
        total_timeout: float = _DEFAULT_TOTAL_TIMEOUT,
        max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
        max_decompressed_bytes: int = _DEFAULT_MAX_DECOMPRESSED_BYTES,
        user_agent: str = _DEFAULT_USER_AGENT,
    ) -> None:
        self._resolve_host = resolve_host
        self._max_redirects = max_redirects
        self._connect_timeout = connect_timeout
        self._total_timeout = total_timeout
        self._max_response_bytes = max_response_bytes
        self._max_decompressed_bytes = max_decompressed_bytes
        self._user_agent = user_agent
        self._client = httpx.Client(
            transport=transport,
            trust_env=False,  # never honour HTTP(S)_PROXY / NO_PROXY / .netrc from the environment
            follow_redirects=False,  # every redirect is revalidated manually, see module docstring
            verify=True,
            timeout=httpx.Timeout(connect_timeout),
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> ArticleFetcher:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def fetch(self, url: str) -> FetchOutcome:
        deadline = time.monotonic() + self._total_timeout
        try:
            with _absolute_fetch_timeout(self._total_timeout):
                return self._fetch_until_deadline(url, deadline)
        except _AbsoluteFetchTimeout:
            return FetchFailure(FetchFailureReason.TIMEOUT, "overall fetch time budget exceeded")

    def _fetch_until_deadline(self, url: str, deadline: float) -> FetchOutcome:
        current_url = url

        for hop in range(self._max_redirects + 1):
            endpoint = _validate_and_resolve(current_url, self._resolve_host)
            if isinstance(endpoint, FetchFailure):
                return endpoint

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return FetchFailure(FetchFailureReason.TIMEOUT, "overall fetch time budget exceeded")

            outcome = self._request_once(endpoint, timeout=min(self._connect_timeout, remaining))
            if isinstance(outcome, FetchFailure):
                return outcome

            status_code, headers, response = outcome
            try:
                if status_code in _REDIRECT_STATUS_CODES:
                    if hop >= self._max_redirects:
                        return FetchFailure(FetchFailureReason.TOO_MANY_REDIRECTS,
                                             f"exceeded {self._max_redirects} redirects")
                    location = headers.get("location")
                    if not location:
                        return FetchFailure(FetchFailureReason.REDIRECT_MISSING_LOCATION,
                                             f"{status_code} response had no Location header")
                    current_url = urljoin(current_url, location)
                    continue

                if status_code != 200:
                    return FetchFailure(
                        FetchFailureReason.HTTP_ERROR,
                        f"unexpected status code {status_code}",
                        status_code=status_code,
                    )

                content_type, charset = _content_type_and_charset(headers)
                if content_type not in _ALLOWED_CONTENT_TYPES:
                    return FetchFailure(FetchFailureReason.UNSUPPORTED_CONTENT_TYPE,
                                         f"content-type {content_type!r} is not HTML-compatible")

                if not _verify_peer(response, endpoint.ip):
                    return FetchFailure(FetchFailureReason.PEER_MISMATCH,
                                         "connected peer address did not match the pinned, validated IP")

                body = _read_capped_body(
                    response,
                    charset,
                    self._max_response_bytes,
                    self._max_decompressed_bytes,
                    deadline,
                )
                if isinstance(body, FetchFailure):
                    return body

                return FetchedArticle(
                    url=current_url, status_code=status_code, content_type=content_type,
                    html=body.text, truncated=body.truncated,
                )
            finally:
                response.close()

        return FetchFailure(FetchFailureReason.TOO_MANY_REDIRECTS, f"exceeded {self._max_redirects} redirects")

    def _request_once(
        self, endpoint: _Endpoint, timeout: float,
    ) -> tuple[int, httpx.Headers, httpx.Response] | FetchFailure:
        ip_for_url = f"[{endpoint.ip}]" if ":" in endpoint.ip else endpoint.ip
        url = httpx.URL(f"{endpoint.scheme}://{ip_for_url}:{endpoint.port}{endpoint.path_and_query}")
        request = self._client.build_request(
            "GET", url,
            headers={
                "Host": endpoint.host_header,
                "User-Agent": self._user_agent,
                "Accept": "text/html,application/xhtml+xml",
            },
            extensions={"sni_hostname": endpoint.sni_hostname, "timeout": {"connect": timeout, "read": timeout,
                                                                            "write": timeout, "pool": timeout}},
        )
        try:
            response = self._client.send(request, stream=True)
        except httpx.TimeoutException as exc:
            return FetchFailure(FetchFailureReason.TIMEOUT, str(exc))
        except httpx.TransportError as exc:
            reason = FetchFailureReason.TLS_ERROR if "ssl" in type(exc).__name__.lower() else \
                FetchFailureReason.CONNECTION_FAILED
            return FetchFailure(reason, str(exc))
        return response.status_code, response.headers, response
