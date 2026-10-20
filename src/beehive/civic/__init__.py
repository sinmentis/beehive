"""Beehive Civic's official source connectors.

Deliberately a separate package from `beehive.connectors` (the Channel/feed plugin seam) but NOT
a parallel architecture: every request still goes through `beehive.connectors.http`, so the SSRF
allow-list, bounded body, retry taxonomy, conditional-GET and Content-Encoding handling that
package already owns apply unchanged. What lives here is only what Civic additionally needs and
Channels do not:

* **Fixed, application-controlled endpoints.** A Civic source is a `CivicSourceDefinition`
  constant in `catalog.py`, never a stored URL and never a value an Owner or a model supplies.
  `validate_config` rejects any non-empty config for the same reason `official_feeds` does: a
  stored URL is a redirect waiting to happen.
* **Three distinct timestamps.** Channels keep one `created_at`; a civic statement has to
  distinguish when the source published it, when the source last changed it, and when Beehive
  first saw it (`documents.CivicDocument`). Cross-day deduplication and "is this actually new"
  both depend on that separation.
* **Explicit source health.** An empty result, a tombstoned endpoint that still answers HTTP
  200, and a partially-degraded crawl are three different outcomes, and none of them may look
  like a successful fetch of zero documents (`documents.CivicSourceHealth`).
* **Evidence tier by application config.** A document's `CivicEvidenceTier` comes from the
  definition in `catalog.py` -- never from remote data, a response field, or a model. A source
  cannot promote itself.

`connectors/feeds.py` also exposes adapters over the existing `beehive.connectors.official_feeds`
definitions, so the RBNZ and NZ Government feeds Channels already fetch are reused here rather
than re-implemented against the same endpoints twice.

Alongside the connectors, this package holds the publication policy those documents feed:
`reasons.py` (the closed reason-code vocabulary), `dedup.py` (the duplicate windows),
`evidence.py` (the evidence, significance and corroboration rules) and `batch_policy.py` (the
per-slot cap). Those four are pure -- no I/O, no clock, no AI -- so a published statement can be
re-justified later from its stored inputs alone.
"""
from __future__ import annotations
