from __future__ import annotations

import gzip
import zlib
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import pytest

from beehive.connectors.http import (
    ConnectorHttpError,
    ConnectorHttpErrorKind,
    fetch_bytes,
    fetch_json,
    fetch_text,
)
from tests.connectors.http_stubs import urlopen_response

_URL = "https://example.com/feed.json"


@pytest.fixture(autouse=True)
def _skip_dns(monkeypatch):
    """Every test here is about HTTP behaviour, not about SSRF classification (which
    tests/test_url_safety.py covers), so resolution is pinned to a public address."""
    monkeypatch.setattr("beehive.url_safety._resolve", lambda hostname: ["93.184.216.34"])


def _http_error(code: int, headers=None) -> HTTPError:
    return HTTPError(_URL, code, "boom", hdrs=headers, fp=None)


# --------------------------------------------------------------------------
# Happy path and validators
# --------------------------------------------------------------------------

def test_a_successful_fetch_returns_the_body_and_its_cache_validators():
    with patch("beehive.connectors.http.urllib.request.urlopen") as urlopen:
        urlopen.return_value = urlopen_response(
            b"payload", headers={"ETag": '"v1"', "Last-Modified": "Wed, 21 Oct 2015 07:28:00 GMT"})
        response = fetch_bytes(_URL)

    assert response.body == b"payload"
    assert response.etag == '"v1"'
    assert response.last_modified == "Wed, 21 Oct 2015 07:28:00 GMT"
    assert response.not_modified is False


def test_accept_encoding_is_pinned_rather_than_left_to_the_client():
    """urllib/httpx advertise whatever codecs happen to be installed, so a transitive dependency
    adding brotli would silently start returning bodies the parsing side cannot decode."""
    with patch("beehive.connectors.http.urllib.request.urlopen") as urlopen:
        urlopen.return_value = urlopen_response(b"x")
        fetch_bytes(_URL)

    assert urlopen.call_args.args[0].get_header("Accept-encoding") == "gzip, deflate"


def test_supplied_validators_become_conditional_request_headers():
    with patch("beehive.connectors.http.urllib.request.urlopen") as urlopen:
        urlopen.return_value = urlopen_response(b"x")
        fetch_bytes(_URL, etag='"v1"', last_modified="Wed, 21 Oct 2015 07:28:00 GMT")

    request = urlopen.call_args.args[0]
    assert request.get_header("If-none-match") == '"v1"'
    assert request.get_header("If-modified-since") == "Wed, 21 Oct 2015 07:28:00 GMT"


def test_a_304_is_a_normal_result_carrying_no_body():
    with patch("beehive.connectors.http.urllib.request.urlopen", side_effect=_http_error(304)):
        response = fetch_bytes(_URL, etag='"v1"')

    assert response.not_modified is True
    assert response.body is None
    assert response.etag == '"v1"'


def test_extra_headers_are_merged_into_the_request():
    with patch("beehive.connectors.http.urllib.request.urlopen") as urlopen:
        urlopen.return_value = urlopen_response(b"x")
        fetch_bytes(_URL, extra_headers={"X-Requested-With": "XMLHttpRequest"})

    assert urlopen.call_args.args[0].get_header("X-requested-with") == "XMLHttpRequest"


# --------------------------------------------------------------------------
# Size cap
# --------------------------------------------------------------------------

def test_a_body_over_the_cap_is_rejected_instead_of_buffered():
    with patch("beehive.connectors.http.urllib.request.urlopen") as urlopen:
        urlopen.return_value = urlopen_response(b"x" * 11)
        with pytest.raises(ConnectorHttpError) as excinfo:
            fetch_bytes(_URL, max_bytes=10)

    assert excinfo.value.kind is ConnectorHttpErrorKind.TOO_LARGE


def test_a_body_exactly_at_the_cap_is_accepted():
    with patch("beehive.connectors.http.urllib.request.urlopen") as urlopen:
        urlopen.return_value = urlopen_response(b"x" * 10)
        assert fetch_bytes(_URL, max_bytes=10).body == b"x" * 10


def test_an_oversized_content_length_is_rejected_before_the_body_is_read():
    with patch("beehive.connectors.http.urllib.request.urlopen") as urlopen:
        response = urlopen_response(b"x", headers={"Content-Length": "999999"})
        urlopen.return_value = response
        with pytest.raises(ConnectorHttpError) as excinfo:
            fetch_bytes(_URL, max_bytes=10)

    assert excinfo.value.kind is ConnectorHttpErrorKind.TOO_LARGE
    response.__enter__.return_value.read.assert_not_called()


def test_a_malformed_content_length_is_not_itself_fatal():
    with patch("beehive.connectors.http.urllib.request.urlopen") as urlopen:
        urlopen.return_value = urlopen_response(b"ok", headers={"Content-Length": "banana"})
        assert fetch_bytes(_URL, max_bytes=10).body == b"ok"


def test_gzip_content_encoding_is_decompressed_before_json_parsing():
    body = gzip.compress(b'{"products": [{"id": 1}]}')
    with patch("beehive.connectors.http.urllib.request.urlopen") as urlopen:
        urlopen.return_value = urlopen_response(
            body,
            headers={"Content-Encoding": "gzip"},
        )

        assert fetch_json(_URL) == {"products": [{"id": 1}]}


def test_deflate_content_encoding_is_decompressed_before_text_decoding():
    body = zlib.compress(b"<html>products</html>")
    with patch("beehive.connectors.http.urllib.request.urlopen") as urlopen:
        urlopen.return_value = urlopen_response(
            body,
            headers={"Content-Encoding": "deflate"},
        )

        assert fetch_text(_URL) == "<html>products</html>"


def test_decompressed_body_over_the_cap_is_rejected():
    body = gzip.compress(b"x" * 1_000)
    assert len(body) < 50
    with patch("beehive.connectors.http.urllib.request.urlopen") as urlopen:
        urlopen.return_value = urlopen_response(
            body,
            headers={"Content-Encoding": "gzip"},
        )

        with pytest.raises(ConnectorHttpError) as excinfo:
            fetch_bytes(_URL, max_bytes=50)

    assert excinfo.value.kind is ConnectorHttpErrorKind.TOO_LARGE


def test_malformed_compressed_body_is_a_protocol_error():
    with patch("beehive.connectors.http.urllib.request.urlopen") as urlopen:
        urlopen.return_value = urlopen_response(
            b"not gzip",
            headers={"Content-Encoding": "gzip"},
        )

        with pytest.raises(ConnectorHttpError) as excinfo:
            fetch_bytes(_URL)

    assert excinfo.value.kind is ConnectorHttpErrorKind.PROTOCOL


def test_unrequested_content_encoding_is_a_protocol_error():
    with patch("beehive.connectors.http.urllib.request.urlopen") as urlopen:
        urlopen.return_value = urlopen_response(
            b"opaque",
            headers={"Content-Encoding": "br"},
        )

        with pytest.raises(ConnectorHttpError) as excinfo:
            fetch_bytes(_URL)

    assert excinfo.value.kind is ConnectorHttpErrorKind.PROTOCOL


# --------------------------------------------------------------------------
# Retries and error classification
# --------------------------------------------------------------------------

@pytest.mark.parametrize("code", [408, 425, 429, 500, 502, 503, 504])
def test_transient_statuses_are_retried_then_classified_transient(code):
    with (
        patch(
            "beehive.connectors.http.urllib.request.urlopen",
            side_effect=[_http_error(code)] * 3,
        ) as urlopen,
        patch("beehive.connectors.http.time.sleep"),
    ):
        with pytest.raises(ConnectorHttpError) as excinfo:
            fetch_bytes(_URL)

    assert urlopen.call_count == 3
    assert excinfo.value.kind is ConnectorHttpErrorKind.TRANSIENT


def test_a_retry_succeeds_without_raising():
    with (
        patch(
            "beehive.connectors.http.urllib.request.urlopen",
            side_effect=[_http_error(503), urlopen_response(b"ok")],
        ) as urlopen,
        patch("beehive.connectors.http.time.sleep"),
    ):
        assert fetch_bytes(_URL).body == b"ok"

    assert urlopen.call_count == 2


@pytest.mark.parametrize(
    "code,kind",
    [
        (401, ConnectorHttpErrorKind.ACCESS_DENIED),
        (403, ConnectorHttpErrorKind.ACCESS_DENIED),
        (404, ConnectorHttpErrorKind.NOT_FOUND),
        (410, ConnectorHttpErrorKind.NOT_FOUND),
        (400, ConnectorHttpErrorKind.PROTOCOL),
    ],
)
def test_permanent_statuses_are_classified_and_not_retried(code, kind):
    with patch(
        "beehive.connectors.http.urllib.request.urlopen", side_effect=_http_error(code)
    ) as urlopen:
        with pytest.raises(ConnectorHttpError) as excinfo:
            fetch_bytes(_URL)

    assert urlopen.call_count == 1
    assert excinfo.value.kind is kind


def test_a_connection_level_failure_is_retried_as_transient():
    """Only HTTPError used to be retried, so `URLError`/timeouts -- the *common* transients --
    lost the source until the next scheduled fetch, up to 24 hours later."""
    with (
        patch(
            "beehive.connectors.http.urllib.request.urlopen",
            side_effect=[URLError("connection reset"), urlopen_response(b"ok")],
        ) as urlopen,
        patch("beehive.connectors.http.time.sleep"),
    ):
        assert fetch_bytes(_URL).body == b"ok"

    assert urlopen.call_count == 2


def test_a_timeout_is_retried_as_transient():
    with (
        patch(
            "beehive.connectors.http.urllib.request.urlopen",
            side_effect=[TimeoutError("timed out"), urlopen_response(b"ok")],
        ),
        patch("beehive.connectors.http.time.sleep"),
    ):
        assert fetch_bytes(_URL).body == b"ok"


def test_backoff_is_jittered_so_sibling_sources_do_not_retry_in_lockstep():
    delays = []
    with (
        patch(
            "beehive.connectors.http.urllib.request.urlopen",
            side_effect=[_http_error(503), _http_error(503), urlopen_response(b"ok")],
        ),
        patch("beehive.connectors.http.time.sleep", side_effect=delays.append),
    ):
        fetch_bytes(_URL)

    assert len(delays) == 2
    assert all(0.0 <= d <= 4.0 for d in delays)


def test_a_short_retry_after_is_honoured_verbatim():
    from email.message import Message

    headers = Message()
    headers["Retry-After"] = "7"
    delays = []
    with (
        patch(
            "beehive.connectors.http.urllib.request.urlopen",
            side_effect=[_http_error(429, headers), urlopen_response(b"ok")],
        ),
        patch("beehive.connectors.http.time.sleep", side_effect=delays.append),
    ):
        fetch_bytes(_URL)

    assert delays == [7.0]


def test_an_absurd_retry_after_is_refused_rather_than_slept_off():
    """A hostile or misconfigured origin must not be able to park a collector process for hours."""
    from email.message import Message

    headers = Message()
    headers["Retry-After"] = "86400"
    with (
        patch(
            "beehive.connectors.http.urllib.request.urlopen",
            side_effect=_http_error(503, headers),
        ) as urlopen,
        patch("beehive.connectors.http.time.sleep") as sleep,
    ):
        with pytest.raises(ConnectorHttpError):
            fetch_bytes(_URL)

    assert urlopen.call_count == 1
    sleep.assert_not_called()


def test_an_unsafe_url_fails_fast_without_any_request():
    with patch("beehive.connectors.http.urllib.request.urlopen") as urlopen:
        with pytest.raises(ConnectorHttpError) as excinfo:
            fetch_bytes("file:///etc/passwd")

    assert excinfo.value.kind is ConnectorHttpErrorKind.UNSAFE_URL
    urlopen.assert_not_called()


def test_the_error_message_carries_its_kind():
    error = ConnectorHttpError(ConnectorHttpErrorKind.NOT_FOUND, "gone")
    assert str(error) == "[not_found] gone"


# --------------------------------------------------------------------------
# Decoding helpers
# --------------------------------------------------------------------------

def test_text_decoding_replaces_bad_bytes_rather_than_losing_the_page():
    with patch("beehive.connectors.http.urllib.request.urlopen") as urlopen:
        urlopen.return_value = urlopen_response(b"caf\xff")
        assert fetch_text(_URL) == "caf\ufffd"


def test_invalid_json_is_reported_as_a_protocol_error():
    with patch("beehive.connectors.http.urllib.request.urlopen") as urlopen:
        urlopen.return_value = urlopen_response(b"<html>maintenance</html>")
        with pytest.raises(ConnectorHttpError) as excinfo:
            fetch_json(_URL)

    assert excinfo.value.kind is ConnectorHttpErrorKind.PROTOCOL
