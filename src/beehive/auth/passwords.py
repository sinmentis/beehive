"""Argon2id password hashing — the only file that imports argon2. There is
exactly one owner password for this whole app (no user table), so this module deliberately has
no concept of a username; callers store the single hash under an app_state key (see
scripts/set_admin_password.py)."""
from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(hashed: str, password: str) -> bool:
    try:
        return _hasher.verify(hashed, password)
    except VerifyMismatchError:
        return False
