"""Civic source connector implementations, one module per access shape.

Split by *how* a source is read rather than by institution, because the failure modes cluster
that way: every JSON search API shares the same paging and envelope-assertion problems, every
sitemap shares the same index/child and crawl-delay problems, and every feed shares the same
guid-vs-link identity problem. `beehive.civic.catalog` is where an institution is bound to one of
these shapes together with the tier the application assigns it.
"""
from __future__ import annotations
