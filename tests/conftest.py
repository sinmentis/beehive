"""Test-wide defaults for environment the app now refuses to start without.

`create_app` raises `InsecureSessionSecretError` when `SESSION_SECRET` is missing or shorter than
32 characters, which is the point -- an empty secret silently makes every admin cookie forgeable.
Production always supplies one (`deploy/quadlet/beehive-web.container` mounts it as a Podman
secret), so supplying one here keeps the tests representative rather than weakening the check.
Tests that exercise the refusal set the variable themselves with `monkeypatch.setenv`.
"""
from __future__ import annotations

import pytest

TEST_SESSION_SECRET = "test-secret-at-least-32-characters-long"


@pytest.fixture(autouse=True)
def _session_secret_env(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", TEST_SESSION_SECRET)
