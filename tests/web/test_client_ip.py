from __future__ import annotations

import pytest

from beehive.web.client_ip import parse_trusted_proxies, resolve_client_ip


class _Headers:
    """Starlette's `request.headers` is case-insensitive; a plain dict is not."""

    def __init__(self, **values):
        self._values = {key.lower().replace("_", "-"): value for key, value in values.items()}

    def get(self, name, default=None):
        return self._values.get(name.lower(), default)


_LOCAL_PROXY = parse_trusted_proxies("10.88.0.1")


def test_a_forwarded_header_is_ignored_when_no_proxy_is_trusted():
    """The default configuration. Trusting the header unconditionally was the actual bug: the
    login lockout keys on this value, so an attacker rotating it never hit the five-failure
    limit."""
    ip = resolve_client_ip("10.88.0.1", _Headers(CF_Connecting_IP="1.2.3.4"), ())
    assert ip == "10.88.0.1"


def test_a_forwarded_header_is_ignored_when_the_peer_is_not_the_trusted_proxy():
    ip = resolve_client_ip("203.0.113.9", _Headers(CF_Connecting_IP="1.2.3.4"), _LOCAL_PROXY)
    assert ip == "203.0.113.9"


def test_a_forwarded_header_is_honoured_when_the_peer_is_the_trusted_proxy():
    ip = resolve_client_ip("10.88.0.1", _Headers(CF_Connecting_IP="1.2.3.4"), _LOCAL_PROXY)
    assert ip == "1.2.3.4"


def test_x_forwarded_for_falls_back_to_its_left_most_entry():
    ip = resolve_client_ip(
        "10.88.0.1", _Headers(X_Forwarded_For="1.2.3.4, 10.0.0.1, 10.0.0.2"), _LOCAL_PROXY)
    assert ip == "1.2.3.4"


def test_cf_connecting_ip_wins_over_x_forwarded_for():
    ip = resolve_client_ip(
        "10.88.0.1",
        _Headers(CF_Connecting_IP="1.2.3.4", X_Forwarded_For="9.9.9.9"),
        _LOCAL_PROXY,
    )
    assert ip == "1.2.3.4"


def test_a_forwarded_value_that_is_not_an_address_falls_back_to_the_peer():
    ip = resolve_client_ip("10.88.0.1", _Headers(CF_Connecting_IP="not-an-ip"), _LOCAL_PROXY)
    assert ip == "10.88.0.1"


def test_a_port_suffix_is_stripped_from_a_forwarded_address():
    ip = resolve_client_ip("10.88.0.1", _Headers(X_Forwarded_For="1.2.3.4:51234"), _LOCAL_PROXY)
    assert ip == "1.2.3.4"


def test_a_bracketed_ipv6_forwarded_address_is_unwrapped():
    ip = resolve_client_ip(
        "10.88.0.1", _Headers(X_Forwarded_For="[2001:db8::1]:443"), _LOCAL_PROXY)
    assert ip == "2001:db8::1"


def test_a_missing_peer_reports_unknown_rather_than_none():
    assert resolve_client_ip(None, _Headers(), ()) == "unknown"


def test_a_cidr_block_covers_every_address_inside_it():
    trusted = parse_trusted_proxies("10.88.0.0/16")
    assert resolve_client_ip(
        "10.88.4.7", _Headers(CF_Connecting_IP="1.2.3.4"), trusted) == "1.2.3.4"
    assert resolve_client_ip(
        "10.89.4.7", _Headers(CF_Connecting_IP="1.2.3.4"), trusted) == "10.89.4.7"


@pytest.mark.parametrize(
    "raw,expected_count",
    [
        (None, 0),
        ("", 0),
        ("   ", 0),
        ("10.0.0.1", 1),
        ("10.0.0.1, 192.168.0.0/24", 2),
        ("10.0.0.1,,192.168.0.0/24", 2),
        ("not-a-network", 0),
        ("10.0.0.1, not-a-network", 1),
    ],
)
def test_trusted_proxy_parsing_drops_unusable_entries_without_raising(raw, expected_count):
    """A typo in an optional hardening variable must not stop the web app from booting."""
    assert len(parse_trusted_proxies(raw)) == expected_count


def test_a_host_bearing_cidr_is_accepted_without_strict_masking():
    assert len(parse_trusted_proxies("10.88.4.7/16")) == 1
