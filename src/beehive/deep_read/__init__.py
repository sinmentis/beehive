"""Deep-read: on-demand long-form article briefs. `fetch.py` and `extract.py` are the
network/content boundary -- SSRF-safe download of a single trusted stored URL, and
offline-only Trafilatura extraction. Neither module persists article text."""
from __future__ import annotations
