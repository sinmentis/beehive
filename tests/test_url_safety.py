from __future__ import annotations

import pytest

from beehive.url_safety import (
    UnsafeUrlError,
    assert_fetchable_url,
    host_is_within,
    is_prohibited_address,
    is_safe_external_href,
    safe_external_href,
)


# --------------------------------------------------------------------------
# Render safety
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/a",
        "http://example.com/a",
        "https://example.com:8443/a",  # a non-standard port is fine to *render*, just not to fetch
    ],
)
def test_http_and_https_urls_render_unchanged(url):
    assert safe_external_href(url) == url
    assert is_safe_external_href(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "JavaScript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "vbscript:msgbox(1)",
        "file:///etc/passwd",
        "/relative/path",
        "",
    ],
)
def test_every_other_scheme_degrades_to_a_non_navigating_anchor(url):
    assert safe_external_href(url) == "#"
    assert is_safe_external_href(url) is False


def test_an_unparseable_url_degrades_rather_than_raising():
    assert safe_external_href("http://[oops") == "#"


# --------------------------------------------------------------------------
# Fetch safety: address classification
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",  # loopback
        "0.0.0.0",  # unspecified
        "10.0.0.5",  # RFC 1918
        "172.16.0.1",  # RFC 1918
        "192.168.1.1",  # RFC 1918
        "169.254.169.254",  # link-local; the cloud metadata endpoint
        "100.64.0.1",  # RFC 6598 carrier-grade NAT
        "224.0.0.1",  # multicast
        "240.0.0.1",  # reserved
        "::1",  # IPv6 loopback
        "fe80::1",  # IPv6 link-local
        "fc00::1",  # IPv6 unique-local
        "::ffff:127.0.0.1",  # IPv4-mapped loopback
        "::ffff:169.254.169.254",  # IPv4-mapped metadata endpoint
    ],
)
def test_non_globally_routable_addresses_are_prohibited(ip):
    assert is_prohibited_address(ip) is True


@pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"])
def test_globally_routable_addresses_are_permitted(ip):
    assert is_prohibited_address(ip) is False


# --------------------------------------------------------------------------
# Fetch safety: host allowlisting
# --------------------------------------------------------------------------

def test_allowlist_matches_the_host_itself_and_its_subdomains():
    allowed = frozenset({"reddit.com"})
    assert host_is_within("reddit.com", allowed) is True
    assert host_is_within("www.reddit.com", allowed) is True
    assert host_is_within("old.reddit.com", allowed) is True


def test_allowlist_matches_on_label_boundaries_not_suffixes():
    """A plain `str.endswith` would let an attacker register `evil-reddit.com` and be treated as
    Reddit, which is the classic way a host allowlist is bypassed."""
    allowed = frozenset({"reddit.com"})
    assert host_is_within("evil-reddit.com", allowed) is False
    assert host_is_within("notreddit.com", allowed) is False
    assert host_is_within("reddit.com.evil.example", allowed) is False


def test_allowlist_ignores_case_and_a_trailing_root_dot():
    allowed = frozenset({"reddit.com"})
    assert host_is_within("WWW.Reddit.COM.", allowed) is True


# --------------------------------------------------------------------------
# Fetch safety: assert_fetchable_url
# --------------------------------------------------------------------------

def _resolving_to(monkeypatch, ip: str):
    monkeypatch.setattr("beehive.url_safety._resolve", lambda hostname: [ip])


def test_a_public_https_url_is_accepted_and_returned_unchanged(monkeypatch):
    _resolving_to(monkeypatch, "93.184.216.34")
    url = "https://example.com/feed.rss"
    assert assert_fetchable_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "gopher://example.com/",
        "ftp://example.com/x",
        "javascript:alert(1)",
    ],
)
def test_non_http_schemes_are_rejected(url):
    with pytest.raises(UnsafeUrlError):
        assert_fetchable_url(url)


def test_a_relative_url_is_rejected():
    with pytest.raises(UnsafeUrlError, match="not an absolute URL"):
        assert_fetchable_url("/just/a/path")


def test_embedded_credentials_are_rejected(monkeypatch):
    """`http://expected.com@127.0.0.1/` reads as expected.com to a human but connects to the
    loopback host after the `@`."""
    _resolving_to(monkeypatch, "93.184.216.34")
    with pytest.raises(UnsafeUrlError, match="userinfo"):
        assert_fetchable_url("https://user:pass@example.com/")


def test_a_non_web_port_is_rejected(monkeypatch):
    _resolving_to(monkeypatch, "93.184.216.34")
    with pytest.raises(UnsafeUrlError, match="not 80 or 443"):
        assert_fetchable_url("http://example.com:6379/")


def test_an_unparseable_port_is_rejected():
    with pytest.raises(UnsafeUrlError):
        assert_fetchable_url("http://example.com:notaport/")


@pytest.mark.parametrize(
    "ip", ["127.0.0.1", "169.254.169.254", "10.1.2.3", "::1"])
def test_a_host_resolving_to_a_prohibited_address_is_rejected(monkeypatch, ip):
    _resolving_to(monkeypatch, ip)
    with pytest.raises(UnsafeUrlError, match="prohibited address"):
        assert_fetchable_url("https://internal.example.com/")


def test_a_literal_loopback_address_is_rejected_without_resolving():
    with pytest.raises(UnsafeUrlError, match="prohibited address"):
        assert_fetchable_url("http://127.0.0.1:80/admin")


def test_a_host_outside_the_allowlist_is_rejected_before_any_resolution(monkeypatch):
    def explode(hostname):
        raise AssertionError("must not resolve a host that failed the allowlist")

    monkeypatch.setattr("beehive.url_safety._resolve", explode)
    with pytest.raises(UnsafeUrlError, match="not one of the allowed hosts"):
        assert_fetchable_url(
            "https://evil.example.com/x", allowed_hosts=frozenset({"reddit.com"}))


def test_an_allowlisted_subdomain_is_accepted(monkeypatch):
    _resolving_to(monkeypatch, "151.101.1.140")
    url = "https://www.reddit.com/r/newzealand/hot/.rss"
    assert assert_fetchable_url(url, allowed_hosts=frozenset({"reddit.com"})) == url


def test_an_unresolvable_host_is_rejected_rather_than_attempted(monkeypatch):
    import socket

    def fail(*args, **kwargs):
        raise socket.gaierror("Name or service not known")

    monkeypatch.setattr("beehive.url_safety.socket.getaddrinfo", fail)
    with pytest.raises(UnsafeUrlError, match="could not resolve"):
        assert_fetchable_url("https://does-not-exist.example/")
