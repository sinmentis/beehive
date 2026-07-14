# tests/auth/test_tokens.py
from beehive.auth.tokens import (generate_session_id, sign_session_id,
                                     verify_signed_session_id)


def test_generate_session_id_is_random_and_reasonably_long():
    a = generate_session_id()
    b = generate_session_id()
    assert a != b
    assert len(a) > 20


def test_sign_and_verify_roundtrip():
    signed = sign_session_id("abc123", "my-secret")
    assert verify_signed_session_id(signed, "my-secret") == "abc123"


def test_verify_rejects_tampered_session_id():
    signed = sign_session_id("abc123", "my-secret")
    tampered = signed.replace("abc123", "xyz789")
    assert verify_signed_session_id(tampered, "my-secret") is None


def test_verify_rejects_wrong_secret():
    signed = sign_session_id("abc123", "my-secret")
    assert verify_signed_session_id(signed, "different-secret") is None


def test_verify_rejects_malformed_input():
    assert verify_signed_session_id("not-a-valid-signed-token", "my-secret") is None
    assert verify_signed_session_id("", "my-secret") is None


def test_verify_rejects_non_ascii_signature_without_raising():
    """Regression guard: hmac.compare_digest raises TypeError on non-ASCII str input, which
    would surface as an unhandled 500 in the session-cookie-checking dependency (a later task)
    instead of a graceful "invalid session" outcome. Comparing as bytes avoids this."""
    assert verify_signed_session_id("abc123.\u00e9\u00e9\u00e9", "my-secret") is None
