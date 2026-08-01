"""Stubs for the one urllib call every connector now shares (`connectors/http.fetch_bytes`).

The stub has to be more faithful than a bare MagicMock: `fetch_bytes` reads `response.headers`
(a MagicMock there would make `int(Content-Length)` raise TypeError rather than the ValueError the
malformed-header path catches) and calls `read(max_bytes + 1)` rather than `read()`.
"""
from __future__ import annotations

from email.message import Message
from unittest.mock import MagicMock


def urlopen_response(body: bytes, *, headers: dict[str, str] | None = None) -> MagicMock:
    message = Message()
    for key, value in (headers or {}).items():
        message[key] = value
    response = MagicMock()
    entered = response.__enter__.return_value
    entered.headers = message
    entered.read.side_effect = lambda *args: body
    return response
