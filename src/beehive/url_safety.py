"""The single home for both URL safety rules this app needs. Deliberately dependency-free (stdlib
only) and at the top of the package, so every layer -- connectors, deep_read, channels, web -- can
import it without any layering inversion. Two distinct rules live here, for two distinct threats:

RENDER safety (`safe_external_href`): may a stored/external URL be emitted into an `<a href>` or
an `<img src>`? Only `http`/`https`; anything else (a `javascript:` URI, an unknown scheme, a bare
relative path smuggled in as "a URL") degrades to "#" rather than being rendered as-is.

FETCH safety (`assert_fetchable_url`): may the server itself send a request to a stored/external
URL? This is the SSRF question, and it is strictly stronger than the render question: on top of
the scheme check it rejects embedded credentials, non-80/443 ports, and any host that resolves to
a non-globally-routable address (loopback, RFC 1918, link-local, RFC 6598 shared space, multicast,
reserved). It also accepts an optional host allowlist for the case where the app knows exactly
which provider a URL is supposed to belong to.

Before this module existed the render rule was written three times (`web/link_safety.py`,
`channels/views.py`, `web/public.py`'s former `_safe_href`) with an explicit "reproduced here
because a lower layer must not import web/" comment, and the fetch rule existed only inside
`deep_read/fetch.py` -- which is why `connectors/reddit.py` could `urlopen` a stored feed URL with
no validation at all.

Note on `assert_fetchable_url` vs `deep_read/fetch.py`: this function answers "is this URL an
eligible target at all", which is what a simple `urlopen`-based connector needs. It does NOT
provide `deep_read`'s DNS pinning (resolve once, connect to that literal IP, verify the peer),
which additionally closes the DNS-rebinding window between validation and connection. A caller
that follows redirects or fetches genuinely arbitrary owner-supplied URLs wants
`deep_read.fetch.ArticleFetcher`; a caller hitting a known provider over a fixed path wants this.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit, urlparse

SAFE_RENDER_SCHEMES = frozenset({"http", "https"})
_FETCH_DEFAULT_PORTS = {"http": 80, "https": 443}
_FETCH_ALLOWED_PORTS = frozenset(_FETCH_DEFAULT_PORTS.values())


class UnsafeUrlError(ValueError):
    """A URL is not an eligible target for a server-side fetch. Raised (never returned as a
    falsy value) so a caller cannot accidentally ignore it by forgetting to check a bool."""


# ============================================================================
# Render safety
# ============================================================================

def safe_external_href(url: str) -> str:
    """Returns `url` unchanged if its scheme is http/https, otherwise "#" -- a template must
    always render this return value as the `href`, never the raw `url`, so an unsafe scheme
    degrades to a non-navigating anchor instead of ever executing or redirecting anywhere."""
    try:
        scheme = urlparse(url).scheme
    except ValueError:
        return "#"
    return url if scheme in SAFE_RENDER_SCHEMES else "#"


def is_safe_external_href(url: str) -> bool:
    """True only if `safe_external_href` would return `url` unchanged -- lets a caller decide
    whether to render a real link vs. plain text without needing to compare strings itself."""
    return safe_external_href(url) == url


# ============================================================================
# Fetch safety (SSRF)
# ============================================================================

def is_prohibited_address(ip_literal: str) -> bool:
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


def _resolve(hostname: str) -> list[str]:
    try:
        ipaddress.ip_address(hostname)
        return [hostname]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(
            hostname, None, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeUrlError(f"could not resolve {hostname!r}: {exc}") from exc
    addresses: list[str] = []
    for *_unused, sockaddr in infos:
        ip = sockaddr[0]
        if ip not in addresses:
            addresses.append(ip)
    if not addresses:
        raise UnsafeUrlError(f"resolver returned no addresses for {hostname!r}")
    return addresses


def host_is_within(hostname: str, allowed_hosts: frozenset[str]) -> bool:
    """True when `hostname` is one of `allowed_hosts` or a subdomain of one. Compared on label
    boundaries (`.` prefix), never `str.endswith` alone, so "evil-reddit.com" does not match
    "reddit.com"."""
    host = hostname.lower().rstrip(".")
    return any(host == allowed or host.endswith(f".{allowed}") for allowed in allowed_hosts)


def assert_fetchable_url(url: str, *, allowed_hosts: frozenset[str] | None = None) -> str:
    """Raises UnsafeUrlError unless `url` is an eligible server-side fetch target. Returns the
    URL unchanged on success so it can be used inline at the call site.

    When `allowed_hosts` is given, the URL's host must equal or be a subdomain of one of them.
    Pass it whenever the caller knows which provider the URL belongs to: it is a far tighter
    control than the address check alone, because it also stops a compromised or malicious feed
    from redirecting the app at an unrelated third party.
    """
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise UnsafeUrlError(f"not a parseable URL: {url!r}") from exc

    if not parts.scheme or not parts.hostname:
        raise UnsafeUrlError(f"not an absolute URL: {url!r}")
    if parts.scheme.lower() not in SAFE_RENDER_SCHEMES:
        raise UnsafeUrlError(f"scheme {parts.scheme!r} is not http/https: {url!r}")
    if parts.username is not None or parts.password is not None:
        raise UnsafeUrlError(f"URL contains embedded userinfo credentials: {url!r}")

    try:
        port = parts.port
    except ValueError as exc:
        raise UnsafeUrlError(f"URL has an invalid port: {url!r}") from exc
    if port is None:
        port = _FETCH_DEFAULT_PORTS[parts.scheme.lower()]
    if port not in _FETCH_ALLOWED_PORTS:
        raise UnsafeUrlError(f"port {port} is not 80 or 443: {url!r}")

    if allowed_hosts is not None and not host_is_within(parts.hostname, allowed_hosts):
        raise UnsafeUrlError(
            f"host {parts.hostname!r} is not one of the allowed hosts "
            f"{sorted(allowed_hosts)!r}: {url!r}")

    prohibited = [ip for ip in _resolve(parts.hostname) if is_prohibited_address(ip)]
    if prohibited:
        raise UnsafeUrlError(
            f"{parts.hostname!r} resolves to a prohibited address ({prohibited[0]}): {url!r}")
    return url
