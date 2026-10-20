# src/beehive/research/structured_response.py
"""Re-export shim: the strict structured-JSON parsing primitives now live in
`beehive.ai.structured_response`, because nothing about them is Research-specific -- they know
only the shape of a model's reply, and every later feature's parser (Beehive Civic's included)
needs exactly the same rules.

Kept as a module rather than rewriting every Research import so `beehive.research`'s public
surface is unchanged, and deliberately re-exporting the *same objects* (not subclasses or
wrappers): `beehive.research.structured_response.StructuredResponseError` IS
`beehive.ai.structured_response.StructuredResponseError`, so an `except`/`isinstance` written
against either import path catches errors raised through the other."""
from __future__ import annotations

from beehive.ai.structured_response import (
    StructuredResponseError,
    bounded_string_list,
    extract_fenced_json_object,
    require_bool,
    require_dict,
    require_exact_keys,
    require_list,
    require_string,
)

__all__ = [
    "StructuredResponseError",
    "bounded_string_list",
    "extract_fenced_json_object",
    "require_bool",
    "require_dict",
    "require_exact_keys",
    "require_list",
    "require_string",
]
