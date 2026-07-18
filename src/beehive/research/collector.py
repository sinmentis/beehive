# src/beehive/research/collector.py
"""Application-controlled execution of an already-validated, already-persisted Research Plan
(ADR-0007): the ONLY place in the Research package that calls a connector's `fetch()`. Every
`beehive.domain.research.ResearchSource` handed to `collect()` has already been through
connector_policy.normalize_and_validate_source(s) (at plan-generation time) and been persisted
via `beehive.db.research_sources.create_research_source` -- this module never validates a
connector_type/config itself, it only executes exactly what the application already approved,
by looking each one up in `beehive.connectors.registry` (the same registry connector_policy.py's
final-authority `validate_config` call uses) and calling its `fetch(config)`.

Per-source failure isolation mirrors ADR-0002's existing rule for the feed collector: one
connector raising -- a network error, a malformed feed, an unexpected exception from a
third-party parsing library, anything -- never aborts the batch. It becomes a typed
`SourceFailure` and every other source in the same call still runs. Only when the whole batch
maps to zero failures do we return zero `SourceFailure`; only when every single source failed do
`all_sources_failed` report True, which orchestrator.py uses to distinguish "one flaky feed" from
"nothing at all came back this round".

Every candidate is tagged with the exact Research Source it came from (`research_source_id`,
`connector_type`) and an application-derived `EvidenceQuality` (`quality_for_connector_type`) --
never something the connector itself claims or the AI proposes -- so downstream enrichment,
clustering, and citation never have to re-derive provenance or guess at source reliability. Each
candidate also carries its source's own `source_topic_hint` (its "query" or "subreddit" config
value, whichever is present) -- enrichment.py's relevance ranking uses this alongside the
Research Question itself, since a Candidate a query-based connector returned is presumptively
about that exact query regardless of what language or phrasing the Research Question used to
reach it.

This module never touches beehive.db: `collect()` takes already-persisted ResearchSource rows in
and returns plain in-memory Candidate/SourceFailure dataclasses out. Persisting a Candidate as a
canonical Evidence Item is enrichment.py's job."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from beehive.connectors.base import RawItem, SourceConnector
from beehive.connectors.registry import get as _default_get_connector
from beehive.domain.research import EvidenceQuality, ResearchSource
from beehive.research.limits import MAX_CANDIDATES_PER_SOURCE

# Deterministic, application-controlled mapping from connector_type to EvidenceQuality
# (CONTEXT.md's Evidence Item quality signal) -- never something a connector or the Research
# Plan AI supplies itself. Kept in exact sync with connector_policy.ALLOWED_CONNECTOR_TYPES;
# an unrecognized connector_type (only possible if that allowlist and this mapping have
# drifted) falls back to the most conservative label, REPORTING, rather than raising, since a
# quality classification is advisory metadata, not a safety gate the way connector_policy's
# validation is.
_CONNECTOR_QUALITY: dict[str, EvidenceQuality] = {
    # Official-institution feeds are the primary source for their own announcements.
    "rbnz_news": EvidenceQuality.PRIMARY,
    "nz_government_news": EvidenceQuality.PRIMARY,
    "federal_reserve_news": EvidenceQuality.PRIMARY,
    # Google News aggregates many publishers' reporting rather than publishing itself.
    "google_news_query": EvidenceQuality.AGGREGATOR,
    # Reddit and Hacker News are community-curated/voted link collections, not reporting.
    "reddit_subreddit": EvidenceQuality.COMMUNITY,
    "hackernews_stories": EvidenceQuality.COMMUNITY,
    "hackernews_query": EvidenceQuality.COMMUNITY,
}


def quality_for_connector_type(connector_type: str) -> EvidenceQuality:
    return _CONNECTOR_QUALITY.get(connector_type, EvidenceQuality.REPORTING)


def _topic_hint(config: dict) -> str | None:
    """The one config value, if any, that names what a Research Source is actually about: a
    query-based connector's own search "query", or a subreddit-feed connector's community name
    (e.g. "LocalLLaMA") -- both are meaningful topic signals, unlike a plain feed selector such
    as hackernews_stories' {"feed": "top"}, which has no topic at all (by design: that connector
    is a generic front-page firehose, not aimed at anything)."""
    return config.get("query") or config.get("subreddit")


@dataclass(frozen=True)
class Candidate:
    """One connector-sourced item, tagged with the exact Research Source it came from. Never
    persisted directly -- enrichment.py turns a Candidate into a canonical Evidence Item via
    beehive.db.evidence_items.upsert_evidence_item, keyed on (research_source_id,
    external_key=raw_item.external_id).

    `source_topic_hint` (see `_topic_hint`) is carried through purely as provenance data, exactly
    like `connector_type`/`quality` -- this module has no opinion on relevance; only
    enrichment.py's `select_relevant_candidates` interprets it."""
    research_source_id: int
    connector_type: str
    quality: EvidenceQuality
    raw_item: RawItem
    source_topic_hint: str | None = None


@dataclass(frozen=True)
class SourceFailure:
    """One Research Source's connector raised instead of returning items. error_code is the
    exception's type name (a stable, typed signal), never a free-form message alone -- detail
    carries the human-readable message, bounded so one pathological exception can't produce an
    unbounded failure record."""
    research_source_id: int
    connector_type: str
    error_code: str
    detail: str


_MAX_DETAIL_LENGTH = 500


@dataclass(frozen=True)
class CollectionOutcome:
    candidates: tuple[Candidate, ...]
    failures: tuple[SourceFailure, ...]

    @property
    def all_sources_failed(self) -> bool:
        """True only when every source that was attempted failed. False for an empty
        `sources` list (nothing was attempted, which is not itself a failure) and False as
        soon as at least one source produced candidates."""
        return bool(self.failures) and not self.candidates


def collect(
    sources: list[ResearchSource],
    *,
    connector_resolver: Callable[[str], SourceConnector] = _default_get_connector,
    max_candidates_per_source: int = MAX_CANDIDATES_PER_SOURCE,
) -> CollectionOutcome:
    """Executes `connector.fetch(source.config)` for exactly the given, already-validated and
    already-persisted Research Sources -- nothing else. `connector_resolver` defaults to the
    real `beehive.connectors.registry.get`, matching connector_policy.py's own final-authority
    connector lookup; tests inject a fake resolver returning stub connectors so this module
    never touches the network or the process-wide connector registry.

    Each source's fetch is independently isolated: any exception (connector lookup failure,
    fetch() raising, a third-party parsing library choking on malformed feed data) is caught
    and becomes one SourceFailure, and collection proceeds with the remaining sources rather
    than aborting the whole batch (mirrors ADR-0002's per-source-per-channel failure
    isolation for the existing feed collector)."""
    candidates: list[Candidate] = []
    failures: list[SourceFailure] = []
    for source in sources:
        try:
            connector = connector_resolver(source.connector_type)
            raw_items = connector.fetch(source.config)
        except Exception as exc:  # noqa: BLE001 -- deliberately broad: isolate, never abort
            failures.append(SourceFailure(
                research_source_id=source.id,
                connector_type=source.connector_type,
                error_code=type(exc).__name__,
                detail=str(exc)[:_MAX_DETAIL_LENGTH],
            ))
            continue
        quality = quality_for_connector_type(source.connector_type)
        topic_hint = _topic_hint(source.config)
        for raw_item in raw_items[:max_candidates_per_source]:
            candidates.append(Candidate(
                research_source_id=source.id,
                connector_type=source.connector_type,
                quality=quality,
                raw_item=raw_item,
                source_topic_hint=topic_hint,
            ))
    return CollectionOutcome(candidates=tuple(candidates), failures=tuple(failures))


__all__ = [
    "Candidate",
    "SourceFailure",
    "CollectionOutcome",
    "quality_for_connector_type",
    "collect",
]
