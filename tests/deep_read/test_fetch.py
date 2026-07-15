import gzip
import time
import zlib

import httpx
import pytest

from beehive.deep_read.fetch import (
    ArticleFetcher,
    DnsResolutionError,
    FetchedArticle,
    FetchFailure,
    FetchFailureReason,
    _is_prohibited_address,
    _verify_peer,
)

_HTML = "<html><body><p>hello world</p></body></html>"


def _html_response(body: bytes = _HTML.encode(), **headers) -> httpx.Response:
    headers.setdefault("content-type", "text/html; charset=utf-8")
    return httpx.Response(200, headers=headers, content=iter([body]))


def _fetcher(resolve_host, handler, **kwargs) -> ArticleFetcher:
    return ArticleFetcher(resolve_host=resolve_host, transport=httpx.MockTransport(handler), **kwargs)


def _unreachable_handler(request):  # a request reaching the transport is itself the test failure
    raise AssertionError(f"transport should never have been called for {request.url!r}")


# ---------------------------------------------------------------------------
# Structural URL validation (no I/O at all should happen for these)
# ---------------------------------------------------------------------------

def test_rejects_non_http_scheme():
    fetcher = _fetcher(lambda h: (_ for _ in ()).throw(AssertionError("no DNS for bad scheme")),
                        _unreachable_handler)
    result = fetcher.fetch("ftp://example.com/file")
    assert isinstance(result, FetchFailure)
    assert result.reason == FetchFailureReason.INVALID_SCHEME


def test_rejects_credentials_in_url():
    fetcher = _fetcher(lambda h: (_ for _ in ()).throw(AssertionError("no DNS")), _unreachable_handler)
    result = fetcher.fetch("http://user:pass@example.com/")
    assert isinstance(result, FetchFailure)
    assert result.reason == FetchFailureReason.CREDENTIALS_IN_URL


def test_rejects_bare_username_without_password():
    fetcher = _fetcher(lambda h: (_ for _ in ()).throw(AssertionError("no DNS")), _unreachable_handler)
    result = fetcher.fetch("http://admin@example.com/")
    assert isinstance(result, FetchFailure)
    assert result.reason == FetchFailureReason.CREDENTIALS_IN_URL


@pytest.mark.parametrize("url", ["http://example.com:8080/", "https://example.com:8443/", "http://example.com:22/"])
def test_rejects_non_standard_ports(url):
    fetcher = _fetcher(lambda h: (_ for _ in ()).throw(AssertionError("no DNS")), _unreachable_handler)
    result = fetcher.fetch(url)
    assert isinstance(result, FetchFailure)
    assert result.reason == FetchFailureReason.INVALID_PORT


@pytest.mark.parametrize("url", ["http://example.com/", "http://example.com:80/",
                                  "https://example.com/", "https://example.com:443/"])
def test_accepts_default_and_explicit_standard_ports(url):
    fetcher = _fetcher(lambda h: ["93.184.216.34"], lambda r: _html_response())
    result = fetcher.fetch(url)
    assert isinstance(result, FetchedArticle)


@pytest.mark.parametrize("url", ["not a url", "example.com/no-scheme", "http:///no-host", ""])
def test_rejects_malformed_urls(url):
    fetcher = _fetcher(lambda h: (_ for _ in ()).throw(AssertionError("no DNS")), _unreachable_handler)
    result = fetcher.fetch(url)
    assert isinstance(result, FetchFailure)
    assert result.reason == FetchFailureReason.MALFORMED_URL


# ---------------------------------------------------------------------------
# Address classification (the core SSRF allow/deny logic)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ip", [
    "127.0.0.1",             # loopback
    "10.0.0.1",               # RFC1918 private
    "172.16.0.5",             # RFC1918 private
    "192.168.1.1",            # RFC1918 private
    "169.254.169.254",        # link-local / cloud metadata endpoint
    "100.64.0.1",             # RFC 6598 shared address space
    "100.100.100.200",        # Alibaba Cloud instance metadata endpoint
    "224.0.0.1",              # multicast
    "240.0.0.1",              # reserved
    "0.0.0.0",                # unspecified
    "::1",                    # IPv6 loopback
    "fc00::1",                # IPv6 unique local (private)
    "fe80::1",                # IPv6 link-local
    "ff02::1",                # IPv6 multicast
    "::",                     # IPv6 unspecified
    "::ffff:127.0.0.1",       # IPv4-mapped IPv6 loopback
    "::ffff:10.0.0.5",        # IPv4-mapped IPv6 private
])
def test_prohibited_addresses_are_rejected(ip):
    assert _is_prohibited_address(ip) is True


@pytest.mark.parametrize("ip", ["93.184.216.34", "8.8.8.8", "2606:4700:4700::1111"])
def test_public_addresses_are_allowed(ip):
    assert _is_prohibited_address(ip) is False


@pytest.mark.parametrize("ip", [
    "127.0.0.1", "10.0.0.1", "172.16.0.5", "192.168.1.1", "169.254.169.254",
    "224.0.0.1", "240.0.0.1", "0.0.0.0", "::1", "fc00::1", "fe80::1", "ff02::1", "::",
])
def test_fetch_rejects_host_resolving_solely_to_prohibited_address(ip):
    fetcher = _fetcher(lambda h: [ip], _unreachable_handler)
    result = fetcher.fetch("http://internal.example/")
    assert isinstance(result, FetchFailure)
    assert result.reason == FetchFailureReason.PROHIBITED_ADDRESS


@pytest.mark.parametrize("addresses", [
    ["93.184.216.34", "10.0.0.5"],           # public first, private second
    ["10.0.0.5", "93.184.216.34"],           # private first, public second
    ["93.184.216.34", "8.8.8.8", "127.0.0.1"],  # multiple public, one prohibited
    ["2606:4700:4700::1111", "fc00::1"],     # public v6 mixed with private v6
])
def test_fetch_rejects_host_with_any_prohibited_address_among_several(addresses):
    fetcher = _fetcher(lambda h: addresses, _unreachable_handler)
    result = fetcher.fetch("http://mixed.example/")
    assert isinstance(result, FetchFailure)
    assert result.reason == FetchFailureReason.PROHIBITED_ADDRESS


def test_dns_resolution_failure_is_a_typed_failure():
    def failing_resolver(host):
        raise DnsResolutionError(f"nxdomain: {host}")

    fetcher = _fetcher(failing_resolver, _unreachable_handler)
    result = fetcher.fetch("http://nowhere.example/")
    assert isinstance(result, FetchFailure)
    assert result.reason == FetchFailureReason.DNS_RESOLUTION_FAILED


# ---------------------------------------------------------------------------
# IP pinning: connection target is the validated IP, Host/SNI stay the hostname
# ---------------------------------------------------------------------------

def test_pins_connection_to_validated_ip_but_keeps_host_header_and_sni():
    seen = {}

    def handler(request):
        seen["url_host"] = request.url.host
        seen["host_header"] = request.headers.get("host")
        seen["sni_hostname"] = request.extensions.get("sni_hostname")
        return _html_response()

    fetcher = _fetcher(lambda h: ["93.184.216.34"], handler)
    result = fetcher.fetch("https://news.example.com/story")

    assert isinstance(result, FetchedArticle)
    assert seen["url_host"] == "93.184.216.34"
    assert seen["host_header"] == "news.example.com"
    assert seen["sni_hostname"] == "news.example.com"


def test_pins_connection_to_validated_ipv6_literal():
    seen = {}

    def handler(request):
        seen["url_host"] = request.url.host
        return _html_response()

    fetcher = _fetcher(lambda h: ["2606:4700:4700::1111"], handler)
    result = fetcher.fetch("https://v6.example.com/")

    assert isinstance(result, FetchedArticle)
    assert seen["url_host"] == "2606:4700:4700::1111"


def test_dns_is_resolved_only_once_per_hop_not_re_resolved_at_connect_time():
    """The anti-rebinding guarantee: resolution happens once, up front, and the resulting IP
    is what the connection is pinned to -- there is no second lookup for a rebinding DNS
    server to answer differently between validation and connection."""
    call_count = 0

    def resolver(host):
        nonlocal call_count
        call_count += 1
        # If this were called again, a rebinding attacker could switch to a private IP here.
        return ["93.184.216.34"] if call_count == 1 else ["127.0.0.1"]

    fetcher = _fetcher(resolver, lambda r: _html_response())
    result = fetcher.fetch("http://rebinding.example/")

    assert isinstance(result, FetchedArticle)
    assert call_count == 1


# ---------------------------------------------------------------------------
# Peer verification (defense in depth on top of IP pinning)
# ---------------------------------------------------------------------------

class _FakeNetworkStream:
    def __init__(self, peername):
        self._peername = peername

    def get_extra_info(self, name):
        return self._peername if name == "peername" else None


def test_verify_peer_accepts_matching_peer():
    response = httpx.Response(200, extensions={"network_stream": _FakeNetworkStream(("93.184.216.34", 443))})
    assert _verify_peer(response, "93.184.216.34") is True


def test_verify_peer_rejects_mismatched_peer():
    response = httpx.Response(200, extensions={"network_stream": _FakeNetworkStream(("10.0.0.9", 443))})
    assert _verify_peer(response, "93.184.216.34") is False


def test_verify_peer_passes_when_peer_info_unavailable():
    response = httpx.Response(200)  # no network_stream extension, e.g. under a mock transport
    assert _verify_peer(response, "93.184.216.34") is True


def test_fetch_fails_closed_on_peer_mismatch():
    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/html"}, content=iter([_HTML.encode()]),
                               extensions={"network_stream": _FakeNetworkStream(("10.0.0.9", 443))})

    fetcher = _fetcher(lambda h: ["93.184.216.34"], handler)
    result = fetcher.fetch("http://rebound.example/")
    assert isinstance(result, FetchFailure)
    assert result.reason == FetchFailureReason.PEER_MISMATCH


# ---------------------------------------------------------------------------
# Redirects: manually revalidated at every hop
# ---------------------------------------------------------------------------

def test_follows_redirect_to_a_safe_host():
    hops = []

    def resolver(host):
        return {"first.example": ["93.184.216.34"], "second.example": ["8.8.8.8"]}[host]

    def handler(request):
        hops.append(request.url.host)
        if request.url.host == "93.184.216.34":
            return httpx.Response(302, headers={"location": "https://second.example/final"})
        return _html_response()

    fetcher = _fetcher(resolver, handler)
    result = fetcher.fetch("https://first.example/start")

    assert isinstance(result, FetchedArticle)
    assert result.url == "https://second.example/final"
    assert hops == ["93.184.216.34", "8.8.8.8"]


def test_redirect_to_prohibited_address_is_rejected():
    def resolver(host):
        return {"public.example": ["93.184.216.34"], "internal.example": ["169.254.169.254"]}[host]

    def handler(request):
        if request.url.host == "93.184.216.34":
            return httpx.Response(302, headers={"location": "http://internal.example/secret"})
        raise AssertionError("must never connect past the redirect")

    fetcher = _fetcher(resolver, handler)
    result = fetcher.fetch("http://public.example/")

    assert isinstance(result, FetchFailure)
    assert result.reason == FetchFailureReason.PROHIBITED_ADDRESS


def test_redirect_chain_exceeding_max_redirects_fails():
    def handler(request):
        return httpx.Response(302, headers={"location": "http://loop.example/next"})

    fetcher = _fetcher(lambda h: ["93.184.216.34"], handler, max_redirects=2)
    result = fetcher.fetch("http://loop.example/")

    assert isinstance(result, FetchFailure)
    assert result.reason == FetchFailureReason.TOO_MANY_REDIRECTS


def test_redirect_missing_location_header_fails():
    fetcher = _fetcher(lambda h: ["93.184.216.34"], lambda r: httpx.Response(302, headers={}))
    result = fetcher.fetch("http://noloc.example/")

    assert isinstance(result, FetchFailure)
    assert result.reason == FetchFailureReason.REDIRECT_MISSING_LOCATION


def test_relative_redirect_location_is_resolved_against_current_url():
    def handler(request):
        if request.url.path == "/start":
            return httpx.Response(301, headers={"location": "/moved"})
        return _html_response()

    fetcher = _fetcher(lambda h: ["93.184.216.34"], handler)
    result = fetcher.fetch("http://example.com/start")

    assert isinstance(result, FetchedArticle)
    assert result.url == "http://example.com/moved"


# ---------------------------------------------------------------------------
# Content-type allow-list
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("content_type", ["text/html", "text/html; charset=utf-8", "application/xhtml+xml"])
def test_accepts_html_compatible_content_types(content_type):
    fetcher = _fetcher(lambda h: ["93.184.216.34"], lambda r: _html_response(**{"content-type": content_type}))
    result = fetcher.fetch("http://example.com/")
    assert isinstance(result, FetchedArticle)


@pytest.mark.parametrize("content_type", ["application/json", "text/plain", "application/pdf", "image/png", ""])
def test_rejects_non_html_content_types(content_type):
    fetcher = _fetcher(lambda h: ["93.184.216.34"], lambda r: _html_response(**{"content-type": content_type}))
    result = fetcher.fetch("http://example.com/")
    assert isinstance(result, FetchFailure)
    assert result.reason == FetchFailureReason.UNSUPPORTED_CONTENT_TYPE


def test_non_2xx_status_is_an_http_error():
    fetcher = _fetcher(lambda h: ["93.184.216.34"],
                        lambda r: httpx.Response(404, headers={"content-type": "text/html"}, content=b"nope"))
    result = fetcher.fetch("http://example.com/missing")
    assert isinstance(result, FetchFailure)
    assert result.reason == FetchFailureReason.HTTP_ERROR


# ---------------------------------------------------------------------------
# Byte / decompressed-byte caps
# ---------------------------------------------------------------------------

def test_raw_response_byte_cap_truncates():
    def handler(request):
        def gen():
            for _ in range(64):
                yield b"a" * 256
        return httpx.Response(200, headers={"content-type": "text/html"}, content=gen())

    fetcher = _fetcher(lambda h: ["93.184.216.34"], handler, max_response_bytes=1000)
    result = fetcher.fetch("http://big.example/")

    assert isinstance(result, FetchedArticle)
    assert result.truncated is True
    assert len(result.html) <= 1000


def test_gzip_content_encoding_is_decompressed_correctly():
    payload = ("<html><body>" + "hello world " * 500 + "</body></html>").encode()
    compressed = gzip.compress(payload)

    def handler(request):
        def gen():  # deliver in small, oddly-sized chunks to exercise incremental decompression
            for i in range(0, len(compressed), 37):
                yield compressed[i:i + 37]
        return httpx.Response(200, headers={"content-type": "text/html", "content-encoding": "gzip"}, content=gen())

    fetcher = _fetcher(lambda h: ["93.184.216.34"], handler)
    result = fetcher.fetch("http://gz.example/")

    assert isinstance(result, FetchedArticle)
    assert result.truncated is False
    assert result.html.encode() == payload


def test_deflate_content_encoding_is_decompressed_correctly():
    payload = b"<html><body>deflate test content</body></html>"
    compressed = zlib.compress(payload)

    fetcher = _fetcher(
        lambda h: ["93.184.216.34"],
        lambda r: httpx.Response(200, headers={"content-type": "text/html", "content-encoding": "deflate"},
                                  content=iter([compressed])),
    )
    result = fetcher.fetch("http://deflate.example/")

    assert isinstance(result, FetchedArticle)
    assert result.html.encode() == payload


def test_decompressed_byte_cap_defeats_a_zip_bomb():
    bomb_payload = b"a" * (5 * 1024 * 1024)  # 5 MiB decompresses from a tiny compressed payload
    compressed = gzip.compress(bomb_payload)
    assert len(compressed) < 10_000  # sanity: this really is a large compression ratio

    fetcher = _fetcher(
        lambda h: ["93.184.216.34"],
        lambda r: httpx.Response(200, headers={"content-type": "text/html", "content-encoding": "gzip"},
                                  content=iter([compressed])),
        max_decompressed_bytes=1024,
    )
    result = fetcher.fetch("http://bomb.example/")

    assert isinstance(result, FetchedArticle)
    assert result.truncated is True
    assert len(result.html) <= 1024


@pytest.mark.parametrize("encoding", ["br", "zstd", "compress", "unknown-encoding"])
def test_unsupported_content_encoding_is_a_typed_failure(encoding):
    fetcher = _fetcher(
        lambda h: ["93.184.216.34"],
        lambda r: httpx.Response(200, headers={"content-type": "text/html", "content-encoding": encoding},
                                  content=iter([b"opaque"])),
    )
    result = fetcher.fetch("http://encoded.example/")

    assert isinstance(result, FetchFailure)
    assert result.reason == FetchFailureReason.UNSUPPORTED_CONTENT_ENCODING


# ---------------------------------------------------------------------------
# Timeouts and transport failures
# ---------------------------------------------------------------------------

def test_connect_timeout_is_a_typed_failure():
    def handler(request):
        raise httpx.ConnectTimeout("simulated timeout", request=request)

    fetcher = _fetcher(lambda h: ["93.184.216.34"], handler)
    result = fetcher.fetch("http://slow.example/")

    assert isinstance(result, FetchFailure)
    assert result.reason == FetchFailureReason.TIMEOUT


def test_connection_error_is_a_typed_failure():
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    fetcher = _fetcher(lambda h: ["93.184.216.34"], handler)
    result = fetcher.fetch("http://down.example/")

    assert isinstance(result, FetchFailure)
    assert result.reason == FetchFailureReason.CONNECTION_FAILED


def test_overall_time_budget_is_enforced_across_redirects():
    def handler(request):
        return httpx.Response(302, headers={"location": "http://loop.example/next"})

    fetcher = _fetcher(lambda h: ["93.184.216.34"], handler, total_timeout=0.0, max_redirects=10)
    result = fetcher.fetch("http://loop.example/")

    assert isinstance(result, FetchFailure)
    assert result.reason == FetchFailureReason.TIMEOUT


def test_overall_time_budget_is_enforced_while_streaming_body(monkeypatch):
    ticks = iter([0.0, 0.0, 0.4, 0.8, 1.2])
    monkeypatch.setattr(
        "beehive.deep_read.fetch.time.monotonic",
        lambda: next(ticks, 1.2),
    )

    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=iter([b"<html>", b"<body>", b"still streaming"]),
        )

    fetcher = _fetcher(
        lambda h: ["93.184.216.34"],
        handler,
        total_timeout=1.0,
    )
    result = fetcher.fetch("http://slow-body.example/")

    assert isinstance(result, FetchFailure)
    assert result.reason == FetchFailureReason.TIMEOUT


def test_overall_time_budget_interrupts_slow_response_headers():
    def handler(request):
        time.sleep(0.2)
        return _html_response()

    fetcher = _fetcher(
        lambda h: ["93.184.216.34"],
        handler,
        total_timeout=0.02,
    )
    started = time.perf_counter()
    result = fetcher.fetch("http://slow-headers.example/")
    elapsed = time.perf_counter() - started

    assert isinstance(result, FetchFailure)
    assert result.reason == FetchFailureReason.TIMEOUT
    assert elapsed < 0.15


def test_overall_timeout_closes_response_obtained_before_deadline():
    class TrackingStream(httpx.SyncByteStream):
        def __init__(self):
            self.closed = False

        def __iter__(self):
            yield _HTML.encode()

        def close(self):
            self.closed = True

    class SlowPeer:
        def get_extra_info(self, name):
            time.sleep(0.2)
            return ("93.184.216.34", 80) if name == "peername" else None

    stream = TrackingStream()

    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            stream=stream,
            extensions={"network_stream": SlowPeer()},
        )

    fetcher = _fetcher(
        lambda h: ["93.184.216.34"],
        handler,
        total_timeout=0.02,
    )
    result = fetcher.fetch("http://slow-peer.example/")

    assert isinstance(result, FetchFailure)
    assert result.reason == FetchFailureReason.TIMEOUT
    assert stream.closed is True


# ---------------------------------------------------------------------------
# Proxy environment variables are always ignored
# ---------------------------------------------------------------------------

def test_environment_proxies_are_disabled_regardless_of_env_vars(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9999")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9999")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9999")

    fetcher = ArticleFetcher(resolve_host=lambda h: ["93.184.216.34"])
    try:
        assert fetcher._client.trust_env is False
        assert fetcher._client._mounts == {}
    finally:
        fetcher.close()


def test_fetch_still_works_normally_with_proxy_env_vars_set(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9999")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9999")

    fetcher = _fetcher(lambda h: ["93.184.216.34"], lambda r: _html_response())
    result = fetcher.fetch("https://example.com/")
    assert isinstance(result, FetchedArticle)


# ---------------------------------------------------------------------------
# Context manager / close()
# ---------------------------------------------------------------------------

def test_context_manager_closes_client():
    with ArticleFetcher(resolve_host=lambda h: ["93.184.216.34"]) as fetcher:
        assert fetcher._client.is_closed is False
    assert fetcher._client.is_closed is True
