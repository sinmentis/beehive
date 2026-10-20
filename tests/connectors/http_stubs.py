"""Stubs for the one urllib call every connector now shares (`connectors/http.fetch_bytes`).

The stub has to be more faithful than a bare MagicMock: `fetch_bytes` reads `response.headers`
(a MagicMock there would make `int(Content-Length)` raise TypeError rather than the ValueError the
malformed-header path catches) and calls `read(max_bytes + 1)` rather than `read()`.
"""
from __future__ import annotations

from email.message import Message
from unittest.mock import MagicMock


def urlopen_response(
    body: bytes, *, headers: dict[str, str] | None = None, url: str | None = None,
) -> MagicMock:
    """A stand-in for the object `urllib.request.urlopen` returns.

    `url` is set explicitly (default None) rather than left to MagicMock's attribute
    auto-creation. `fetch_bytes` reads `response.url` to populate `HttpResponse.final_url`, and an
    auto-created Mock is truthy -- so without this every stubbed fetch would hand callers a
    MagicMock where they expect a URL string, and any assertion about the landing host would pass
    against nonsense. None makes `fetch_bytes` fall back to the requested URL, which is what a
    non-redirecting response means; pass a string to simulate a redirect chain.
    """
    message = Message()
    for key, value in (headers or {}).items():
        message[key] = value
    response = MagicMock()
    entered = response.__enter__.return_value
    entered.headers = message
    entered.url = url
    entered.read.side_effect = lambda *args: body
    return response
