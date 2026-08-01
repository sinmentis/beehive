"""Who the login rate limiter counts failures against.

`admin.py` previously read `CF-Connecting-IP` unconditionally. That header is set by Cloudflare,
but nothing stops a client that can reach the app from sending its own: the app publishes on
`127.0.0.1:8095` and any reverse proxy in front of it is expected to overwrite the header, yet a
request that arrives by any other path (a second proxy, a misconfigured one, an SSH tunnel, a
container on the same network) carries whatever the client typed. Since `is_locked_out` keys on
this value, a spoofed header meant every attempt looked like it came from a brand-new IP, so the
five-failure lockout never fired and the password could be brute-forced.

The rule here: a forwarded header is honoured only when the *direct peer* -- the address the TCP
connection actually came from, which cannot be forged -- is in `TRUSTED_PROXY_IPS`. With that
variable unset the header is ignored entirely, so the insecure configuration is also the default
one. Note that the peer seen inside a container is the container network's gateway, not the
proxy's public address; `deploy/README.md` covers what to put in the variable.
"""
from __future__ import annotations

import ipaddress

UNKNOWN_CLIENT_IP = "unknown"
# In order of preference. CF-Connecting-IP is a single address; X-Forwarded-For is a
# comma-separated chain whose *first* entry is the original client.
_FORWARDED_HEADERS = ("CF-Connecting-IP", "X-Forwarded-For")


def parse_trusted_proxies(raw: str | None) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parses the `TRUSTED_PROXY_IPS` value: a comma-separated list of addresses or CIDR blocks.

    An unparseable entry is dropped rather than raising, because the alternative is a web app
    that refuses to boot over a typo in an optional hardening variable.
    """
    if not raw:
        return ()
    networks = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            continue
    return tuple(networks)


def _peer_is_trusted(peer: str | None, trusted_proxies) -> bool:
    if not peer or not trusted_proxies:
        return False
    try:
        address = ipaddress.ip_address(peer)
    except ValueError:
        return False
    return any(address in network for network in trusted_proxies)


def _first_forwarded_address(header_value: str) -> str | None:
    """The left-most entry of an X-Forwarded-For style chain, which is the original client."""
    for candidate in header_value.split(","):
        candidate = candidate.strip()
        if not candidate:
            continue
        # Strip an optional port; IPv6 in this position may or may not be bracketed.
        if candidate.startswith("["):
            candidate = candidate[1:].split("]", 1)[0]
        elif candidate.count(":") == 1:
            candidate = candidate.split(":", 1)[0]
        try:
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            return None
    return None


def resolve_client_ip(peer: str | None, headers, trusted_proxies) -> str:
    """The address to attribute this request to.

    `headers` is anything with a case-insensitive `.get(name)`, i.e. Starlette's `request.headers`.
    """
    if _peer_is_trusted(peer, trusted_proxies):
        for name in _FORWARDED_HEADERS:
            value = headers.get(name)
            if not value:
                continue
            forwarded = _first_forwarded_address(value)
            if forwarded is not None:
                return forwarded
    return peer or UNKNOWN_CLIENT_IP
