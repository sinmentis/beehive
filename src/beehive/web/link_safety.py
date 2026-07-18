"""Shared external-URL scheme validation for every page that renders a link built from
stored/external data (a feed Item's own URL, a Research Evidence Item's source URL, and a
Research Synthesis/Conversation citation's URL). Only `http`/`https` are ever considered safe to
render as a real link -- anything else (a `javascript:` URI, a bare relative path smuggled in as
"a URL", an unknown scheme) degrades to a non-link ("#") rather than being rendered as-is, so a
template's `<a href="...">` can never execute attacker-controlled script or navigate somewhere
unexpected.

public.py's own former `_safe_href` was the first instance of this check (Item URLs); this
module is that same one-line rule promoted to a single shared place so research_view.py's
Evidence Item/citation links reuse the exact rule rather than a second, possibly-drifting
copy."""
from __future__ import annotations

from urllib.parse import urlparse

_SAFE_SCHEMES = frozenset({"http", "https"})


def safe_external_href(url: str) -> str:
    """Returns `url` unchanged if its scheme is http/https, otherwise "#" -- a template must
    always render this return value as the `href`, never the raw `url`, so an unsafe scheme
    degrades to a non-navigating anchor instead of ever executing or redirecting anywhere."""
    try:
        scheme = urlparse(url).scheme
    except ValueError:
        return "#"
    return url if scheme in _SAFE_SCHEMES else "#"


def is_safe_external_href(url: str) -> bool:
    """True only if `safe_external_href` would return `url` unchanged -- lets a caller decide
    whether to render a real link vs. plain text without needing to compare strings itself."""
    return safe_external_href(url) == url
