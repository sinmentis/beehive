"""Photo URLs sized for where they show.

Store and auction photos come straight from each retailer's CDN, often as originals thousands of
pixels wide. A CDN whose URLs take a size is asked for one close to the display size; any other
URL passes through unchanged, so a photo is never lost to a guessed rewrite."""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# END. crops every photo square and names the crop in the path:
# .../f_auto,q_auto:eco,w_400,h_400/...
_END_SIZE_RE = re.compile(r"(?<=[/,])w_\d+,h_\d+(?=[,/])")
# YOOX serves each photo in fixed sizes named by a code before the view letter: ..._11_f.jpg.
_YOOX_SIZE_RE = re.compile(r"_1[0-6]_([a-z])\.jpg$")
# Code 11 is 306px wide, 13 is 550px and 14 is 1571px; each serves requests up to a little past
# its own width.
_YOOX_SIZES = ((340, "11"), (640, "13"))
_YOOX_LARGEST = "14"


def _is_shopify(host: str, path: str) -> bool:
    """Shopify serves product photos from its CDN host or from a store's own /cdn/shop/ path;
    both take a `width` parameter."""
    return host == "cdn.shopify.com" or path.startswith("/cdn/shop/")


def sized_image_url(url: str | None, width: int) -> str | None:
    """`url` asking its CDN for a copy about `width` pixels wide, or `url` unchanged when its
    CDN takes no size."""
    if not url:
        return url
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if _is_shopify(host, parts.path):
        query = [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key != "width"
        ]
        query.append(("width", str(width)))
        return urlunsplit(parts._replace(query=urlencode(query)))
    if host == "media.endclothing.com":
        path = _END_SIZE_RE.sub(f"w_{width},h_{width}", parts.path, count=1)
        return urlunsplit(parts._replace(path=path))
    if host == "yoox.com" or host.endswith(".yoox.com"):
        code = next((code for limit, code in _YOOX_SIZES if width <= limit), _YOOX_LARGEST)
        path = _YOOX_SIZE_RE.sub(rf"_{code}_\1.jpg", parts.path)
        return urlunsplit(parts._replace(path=path))
    return url


def image_srcset(url: str | None, widths: tuple[int, ...]) -> str | None:
    """A `srcset` offering `url` at each width, for a CDN that resizes to any width; None for
    every other URL, which then loads at its one size."""
    if not url:
        return None
    parts = urlsplit(url)
    if not _is_shopify((parts.hostname or "").lower(), parts.path):
        return None
    return ", ".join(f"{sized_image_url(url, width)} {width}w" for width in widths)
