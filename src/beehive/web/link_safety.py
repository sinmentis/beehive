"""Kept as the web layer's import site for the shared render-safety rule, which now lives in
`beehive.url_safety` alongside the fetch/SSRF rule. The rule itself was previously written out
three times (here, `channels/views.py`, and `web/public.py`'s former `_safe_href`), each copy
carrying a comment explaining that a lower layer must not import `web/`; moving it to the top of
the package removed that constraint, and this shim keeps the existing `web/` imports working."""
from __future__ import annotations

from beehive.url_safety import is_safe_external_href, safe_external_href

__all__ = ["is_safe_external_href", "safe_external_href"]
