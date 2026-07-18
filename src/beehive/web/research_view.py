# src/beehive/web/research_view.py
"""View-model + presentation Module for the owner-only Research workspace (web/research.py's
route bodies stay thin and call only into here for anything that turns repository/domain rows
into template-safe data). This module owns every safety-sensitive rendering decision so it lives
in exactly one place:

1. Templates must NEVER receive a raw stored JSON blob, an Evidence Item's `full_text`, a worker
   `claim_token`, or Conversation Memory content -- every function below returns typed,
   already-validated dataclasses/dicts built from repository rows, and a malformed stored value
   (a corrupt `plan_json`, an unknown enum) degrades to a localized "unavailable" shape exactly
   like web/deep_read_view.py's own DeepReadCacheError convention, never a raw exception or the
   raw stored text. The one deliberate exception is a FAILED Research Run's own `error_detail`:
   `build_run_status_view` exposes it as its own labelled technical/diagnostic field (never
   folded into `error_message`'s localized copy), because it is already sanitized at the one
   place it is ever written -- orchestrator.py's synthesis-failure capture and collector/
   research_worker.py's `_classify_error` both store only the causing exception's own type name
   and message, capped (`research.limits.MAX_ERROR_DETAIL_LENGTH`), never the Research Question,
   evidence, or a raw prompt.
2. Every external citation/source link is built through link_safety.safe_external_href -- an
   invalid scheme degrades to a non-link ("#") rather than ever being rendered as-is.
3. Conversation Message content is the one place stored text embeds citation markers
   (`beehive.research.conversation._render_reply_content`'s "[N]" convention) -- `build_message_
   view` is the only place that text is parsed, and a bracket number that is not one of the
   message's own persisted citations is rendered back as inert plain text, never linked and
   never treated as an error worth surfacing.
4. Research Synthesis claims never embed citation markers in `text` (each claim's citations are
   already a separate, resolved list -- see `beehive.research.synthesis.SynthesisFinding`), so
   `build_synthesis_view` only ever needs to attach citation links after the claim text, never
   parse it."""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from beehive.db.evidence_clusters import list_evidence_clusters
from beehive.db.evidence_curation import list_evidence_curation
from beehive.db.evidence_items import get_evidence_items
from beehive.db.evidence_state import get_latest_evidence_state_revision
from beehive.db.research_chat_requests import (ChatRequest, ChatRequestStatus,
                                                get_active_chat_request, list_chat_requests)
from beehive.db.research_messages import list_message_citations, list_messages
from beehive.db.research_plan_revisions import list_plan_revisions
from beehive.db.research_snapshots import list_snapshot_item_ids, list_snapshots
from beehive.db.research_sources import list_research_sources
from beehive.db.research_syntheses import list_syntheses
from beehive.domain.research import (ConversationMessage, ConversationRole, EvidenceItem,
                                      EvidenceQuality, EvidenceSnapshotStatus, ResearchRun,
                                      ResearchRunPhase, ResearchRunStatus, ResearchSession,
                                      ResearchSessionStatus, ResearchSource)
from beehive.localization import Localizer
from beehive.research.synthesis import ResearchSynthesisDocument, build_document
from beehive.web.formatting import host_local_time_label, relative_time
from beehive.web.hackernews_labels import hackernews_source_label
from beehive.web.link_safety import safe_external_href
from beehive.web.official_feed_labels import official_feed_label

# Evidence snippet display bound: independent of research.limits' prompt-projection bounds --
# this only ever trims what the Owner is shown in the Evidence tab, never what is stored or fed
# to a prompt.
MAX_SNIPPET_DISPLAY_CHARS = 320

# Research Question / search keyword display/report bounds shared with web/research.py's create-
# session form validation, kept here since both the form and its rendered summary need the same
# ceiling.
MAX_QUESTION_LENGTH = 500
MAX_KEYWORD_LENGTH = 200
MAX_SUBREDDIT_LENGTH = 100

_CITATION_MARKER_RE = re.compile(r"\[(\d+)\]")


# ============================================================================
# Small localized labels
# ============================================================================

def quality_label(quality: EvidenceQuality, t: Localizer) -> str:
    return t.text(f"web.research.quality.{quality.value}")


def session_status_label(status: ResearchSessionStatus, t: Localizer) -> str:
    return t.text(f"web.research.session_status.{status.value}")


def run_status_label(status: ResearchRunStatus, t: Localizer) -> str:
    return t.text(f"web.research.run_status.{status.value}")


def run_phase_label(phase: ResearchRunPhase | None, t: Localizer) -> str:
    if phase is None:
        return t.text("web.research.phase.none")
    return t.text(f"web.research.phase.{phase.value}")


def _truncate(text: str, max_chars: int) -> str:
    stripped = text.strip()
    if len(stripped) <= max_chars:
        return stripped
    return stripped[:max_chars].rstrip() + "…"


def _connector_label(connector_type: str, config: dict, t: Localizer) -> str:
    """Mirrors web/public.py's `_source_label`/channel `_source_summary` convention for the same
    seven connector types, adapted to a Research Source's own (connector_type, config) shape
    (never an item-joined row)."""
    if connector_type == "reddit_subreddit":
        return f"r/{config.get('subreddit', '')}"
    if connector_type == "google_news_query":
        return t.text("web.research.connector.google_news_query", query=config.get("query", ""))
    official_label = official_feed_label(connector_type)
    if official_label is not None:
        return official_label
    hackernews_label = hackernews_source_label(connector_type, config, t)
    if hackernews_label is not None:
        return hackernews_label
    return connector_type


def _publisher_label(source: ResearchSource | None, item: EvidenceItem, t: Localizer) -> str:
    """The Evidence Item's own originating publisher -- distinct from which connector collected
    it. Google News' raw_metadata carries the actual outlet name when present; every other
    connector type falls back to its own connector label (a subreddit community, an official
    institution's name, or Hacker News' own feed/query label)."""
    if source is not None and source.connector_type == "google_news_query":
        source_name = item.raw_metadata.get("source_name") if item.raw_metadata else None
        if isinstance(source_name, str) and source_name.strip():
            return source_name.strip()
    if source is not None:
        return _connector_label(source.connector_type, source.config, t)
    return t.text("web.research.evidence.publisher_unknown")


# ============================================================================
# Research Session list
# ============================================================================

@dataclass(frozen=True)
class SessionRowView:
    id: int
    question: str
    status_label: str
    is_archived: bool
    last_activity_label: str
    detail_url: str


def build_session_row(session: ResearchSession, t: Localizer) -> SessionRowView:
    return SessionRowView(
        id=session.id,
        question=_truncate(session.question, MAX_QUESTION_LENGTH),
        status_label=session_status_label(session.status, t),
        is_archived=session.status is ResearchSessionStatus.ARCHIVED,
        last_activity_label=relative_time(session.last_activity_at.isoformat(), t),
        detail_url=f"/research/{session.id}",
    )


# ============================================================================
# Run status widget (polled by GET /research/{id}/status)
# ============================================================================

_NON_TERMINAL_RUN_STATUSES = frozenset({ResearchRunStatus.PENDING, ResearchRunStatus.PROCESSING})


@dataclass(frozen=True)
class RunStatusView:
    has_run: bool
    is_pending: bool
    status_label: str
    phase_label: str | None
    requested_at_label: str
    can_cancel: bool
    error_message: str | None
    error_detail: str | None


_RUN_ERROR_KEYS = frozenset({
    "planning_failed", "collection_failed", "synthesis_failed", "deadline_exceeded",
})


def _run_error_message(run: ResearchRun, t: Localizer) -> str | None:
    """Never reads run.error_detail (a raw internal string) -- only the safe, closed set of
    error_code values maps to localized copy; anything else (including a missing/unrecognized
    code) degrades to one generic unavailable message."""
    if run.status is not ResearchRunStatus.FAILED:
        return None
    return t.text("web.research.run_error.generic")


def _run_error_detail(run: ResearchRun) -> str | None:
    """The one deliberate reader of `run.error_detail` in this module -- see the module
    docstring's rule 1 for why this is safe: it is already capped to the causing exception's own
    type name and message, never the Research Question, evidence, or a raw prompt. Returned only
    for a FAILED run (matches `domain.research.ResearchRun.__post_init__`'s own invariant that
    error_code/error_detail are only ever set alongside FAILED), and only when a value was
    actually captured -- older rows (or a failure path that never captures one, e.g. a lost
    claim) still show only `error_message`'s generic copy with no technical detail beneath it."""
    if run.status is not ResearchRunStatus.FAILED:
        return None
    return run.error_detail


def build_run_status_view(run: ResearchRun | None, t: Localizer) -> RunStatusView:
    if run is None:
        return RunStatusView(
            has_run=False, is_pending=False, status_label="", phase_label=None,
            requested_at_label="", can_cancel=False, error_message=None, error_detail=None)
    is_pending = run.status in _NON_TERMINAL_RUN_STATUSES
    return RunStatusView(
        has_run=True,
        is_pending=is_pending,
        status_label=run_status_label(run.status, t),
        phase_label=run_phase_label(run.phase, t) if run.status is ResearchRunStatus.PROCESSING
        else None,
        requested_at_label=relative_time(run.requested_at.isoformat(), t),
        can_cancel=is_pending and not run.cancel_requested,
        error_message=_run_error_message(run, t),
        error_detail=_run_error_detail(run),
    )


# ============================================================================
# Plan tab: strict parsing of stored plan_json, degrading safely on any malformed row
# ============================================================================

@dataclass(frozen=True)
class PlanSourceView:
    label: str
    rationale: str


@dataclass(frozen=True)
class PlanRevisionView:
    version: int
    summary: str
    sources: tuple[PlanSourceView, ...]
    is_available: bool
    created_at_label: str


def _parse_plan_sources(raw_sources: object, t: Localizer) -> tuple[PlanSourceView, ...] | None:
    if not isinstance(raw_sources, list):
        return None
    views = []
    for raw_source in raw_sources:
        if not isinstance(raw_source, dict):
            return None
        connector_type = raw_source.get("connector_type")
        config = raw_source.get("config")
        rationale = raw_source.get("rationale")
        if (not isinstance(connector_type, str) or not isinstance(config, dict)
                or not isinstance(rationale, str)):
            return None
        views.append(PlanSourceView(
            label=_connector_label(connector_type, config, t),
            rationale=_truncate(rationale, 300)))
    return tuple(views)


def build_plan_revision_view(run_id: int, plan_json: str, is_validated: bool, version: int,
                              created_at: datetime, t: Localizer) -> PlanRevisionView:
    """Strictly parses one stored plan_json row (written by
    beehive.research.orchestrator._plan_to_json: {"plan_summary": str, "sources": [{
    "connector_type", "config", "rationale"}, ...]}). Any malformed shape -- not validated,
    corrupt JSON, wrong field types -- degrades to a localized "unavailable" PlanRevisionView
    (is_available=False) rather than raising or ever exposing the raw JSON to a template."""
    unavailable = PlanRevisionView(
        version=version, summary=t.text("web.research.plan.unavailable"), sources=(),
        is_available=False, created_at_label=host_local_time_label(created_at.isoformat()))
    if not is_validated:
        return unavailable
    try:
        data = json.loads(plan_json)
    except (TypeError, ValueError):
        return unavailable
    if not isinstance(data, dict):
        return unavailable
    summary = data.get("plan_summary")
    if not isinstance(summary, str) or not summary.strip():
        return unavailable
    sources = _parse_plan_sources(data.get("sources"), t)
    if sources is None:
        return unavailable
    return PlanRevisionView(
        version=version, summary=_truncate(summary, 600), sources=sources, is_available=True,
        created_at_label=host_local_time_label(created_at.isoformat()))


def build_plan_views(conn, run_id: int, t: Localizer) -> tuple[PlanRevisionView, ...]:
    revisions = list_plan_revisions(conn, run_id)
    return tuple(
        build_plan_revision_view(
            run_id, r.plan_json, r.is_validated, r.version, r.created_at, t)
        for r in revisions)


# ============================================================================
# Evidence tab: clusters + standalone items, quality/publisher/state, exclude/restore actions
# ============================================================================

@dataclass(frozen=True)
class EvidenceItemView:
    id: int
    citation_number: int
    title: str
    snippet: str
    publisher: str
    quality_label: str
    href: str
    is_excluded: bool
    exclude_url: str
    restore_url: str


@dataclass(frozen=True)
class EvidenceClusterView:
    key: str
    is_duplicate_group: bool
    items: tuple[EvidenceItemView, ...]


def _latest_sealed_snapshot_id(conn, session_id: int) -> int | None:
    for snapshot in reversed(list_snapshots(conn, session_id)):
        if snapshot.status is EvidenceSnapshotStatus.SEALED:
            return snapshot.id
    return None


def _build_evidence_item_view(
        item: EvidenceItem, source: ResearchSource | None, is_excluded: bool, session_id: int,
        t: Localizer) -> EvidenceItemView:
    return EvidenceItemView(
        id=item.id,
        citation_number=item.citation_number,
        title=item.title,
        snippet=_truncate(item.snippet, MAX_SNIPPET_DISPLAY_CHARS),
        publisher=_publisher_label(source, item, t),
        quality_label=quality_label(item.quality, t),
        href=safe_external_href(item.url),
        is_excluded=is_excluded,
        exclude_url=f"/research/{session_id}/evidence/{item.id}/exclude",
        restore_url=f"/research/{session_id}/evidence/{item.id}/restore",
    )


@dataclass(frozen=True)
class EvidenceTabView:
    clusters: tuple[EvidenceClusterView, ...]
    has_any_evidence: bool
    all_excluded: bool
    total_items: int = 0
    page: int = 1
    total_pages: int = 1
    has_prev: bool = False
    has_next: bool = False


_EVIDENCE_PAGE_SIZE = 10


def build_evidence_tab_view(conn, session_id: int, t: Localizer, page: int = 1) -> EvidenceTabView:
    snapshot_id = _latest_sealed_snapshot_id(conn, session_id)
    if snapshot_id is None:
        return EvidenceTabView(clusters=(), has_any_evidence=False, all_excluded=False)

    item_ids = list_snapshot_item_ids(conn, snapshot_id)
    if not item_ids:
        return EvidenceTabView(clusters=(), has_any_evidence=False, all_excluded=False)

    items_by_id = get_evidence_items(conn, item_ids)
    curation = list_evidence_curation(conn, item_ids)
    sources_by_id = {s.id: s for s in list_research_sources(conn, session_id)}

    def _is_excluded(item_id: int) -> bool:
        entry = curation.get(item_id)
        return entry is not None and entry.is_excluded

    ordered_ids = sorted(item_ids, key=lambda i: items_by_id[i].citation_number
                          if i in items_by_id else 0)
    clusters_raw = list_evidence_clusters(conn, snapshot_id)
    cluster_of: dict[int, int] = {}
    for cluster in clusters_raw:
        for member_id in cluster.evidence_item_ids:
            cluster_of[member_id] = cluster.id

    seen: set[int] = set()
    cluster_views: list[EvidenceClusterView] = []
    for cluster in clusters_raw:
        member_ids = [i for i in cluster.evidence_item_ids if i in items_by_id]
        if not member_ids or all(i in seen for i in member_ids):
            continue
        seen.update(member_ids)
        member_ids_sorted = sorted(member_ids, key=lambda i: items_by_id[i].citation_number)
        views = tuple(
            _build_evidence_item_view(
                items_by_id[i], sources_by_id.get(items_by_id[i].research_source_id),
                _is_excluded(i), session_id, t)
            for i in member_ids_sorted)
        cluster_views.append(EvidenceClusterView(
            key=f"cluster-{cluster.id}", is_duplicate_group=True, items=views))

    for item_id in ordered_ids:
        if item_id in seen or item_id not in items_by_id:
            continue
        seen.add(item_id)
        view = _build_evidence_item_view(
            items_by_id[item_id], sources_by_id.get(items_by_id[item_id].research_source_id),
            _is_excluded(item_id), session_id, t)
        cluster_views.append(EvidenceClusterView(
            key=f"item-{item_id}", is_duplicate_group=False, items=(view,)))

    # Re-sort the final list by each group's lowest citation_number so clustered and standalone
    # entries interleave in one stable, citation-number-ascending reading order.
    cluster_views.sort(key=lambda c: min(v.citation_number for v in c.items))

    revision = get_latest_evidence_state_revision(conn, session_id)
    active_ids = set(revision.evidence_item_ids) if revision is not None else set()
    all_excluded = len(item_ids) > 0 and not active_ids

    # Evidence sets can grow into the thousands over repeated refreshes, so only one page of
    # clusters is ever sent to the template; total_items still counts every item across all
    # pages so the header's result-stat count stays accurate regardless of the current page.
    total_items = sum(len(c.items) for c in cluster_views)
    total_pages = max(1, math.ceil(len(cluster_views) / _EVIDENCE_PAGE_SIZE))
    safe_page = min(max(page, 1), total_pages)
    start = (safe_page - 1) * _EVIDENCE_PAGE_SIZE
    page_clusters = cluster_views[start:start + _EVIDENCE_PAGE_SIZE]

    return EvidenceTabView(
        clusters=tuple(page_clusters), has_any_evidence=True, all_excluded=all_excluded,
        total_items=total_items, page=safe_page, total_pages=total_pages,
        has_prev=safe_page > 1, has_next=safe_page < total_pages)


# ============================================================================
# Synthesis tab: wraps beehive.research.synthesis.ResearchSynthesisDocument for rendering
# ============================================================================

@dataclass(frozen=True)
class CitationLinkView:
    citation_number: int
    title: str
    href: str
    quality_label: str


@dataclass(frozen=True)
class SynthesisFindingView:
    text: str
    citations: tuple[CitationLinkView, ...]


@dataclass(frozen=True)
class SynthesisTabView:
    has_synthesis: bool
    version: int | None
    sufficiency_label: str | None
    bottom_line: tuple[SynthesisFindingView, ...]
    key_findings: tuple[SynthesisFindingView, ...]
    source_agreements: tuple[SynthesisFindingView, ...]
    source_conflicts: tuple[SynthesisFindingView, ...]
    unknowns: tuple[SynthesisFindingView, ...]
    evidence_coverage: tuple[SynthesisFindingView, ...]
    model_knowledge: tuple[str, ...]
    created_at_label: str | None


def _finding_view(finding, t: Localizer) -> SynthesisFindingView:
    return SynthesisFindingView(
        text=finding.text,
        citations=tuple(
            CitationLinkView(
                citation_number=c.citation_number, title=c.title,
                href=safe_external_href(c.url), quality_label=quality_label(c.quality, t))
            for c in finding.citations),
    )


def build_synthesis_tab_view(document: ResearchSynthesisDocument | None,
                              t: Localizer) -> SynthesisTabView:
    if document is None:
        return SynthesisTabView(
            has_synthesis=False, version=None, sufficiency_label=None, bottom_line=(),
            key_findings=(), source_agreements=(), source_conflicts=(), unknowns=(),
            evidence_coverage=(), model_knowledge=(), created_at_label=None)
    return SynthesisTabView(
        has_synthesis=True,
        version=document.version,
        sufficiency_label=t.text(f"web.research.sufficiency.{document.sufficiency_state.value}"),
        bottom_line=tuple(_finding_view(f, t) for f in document.bottom_line),
        key_findings=tuple(_finding_view(f, t) for f in document.key_findings),
        source_agreements=tuple(_finding_view(f, t) for f in document.source_agreements),
        source_conflicts=tuple(_finding_view(f, t) for f in document.source_conflicts),
        unknowns=tuple(_finding_view(f, t) for f in document.unknowns),
        evidence_coverage=tuple(_finding_view(f, t) for f in document.evidence_coverage),
        model_knowledge=tuple(note.text for note in document.model_knowledge),
        created_at_label=host_local_time_label(document.created_at.isoformat()),
    )


def load_synthesis_document(conn, session_id: int) -> ResearchSynthesisDocument | None:
    syntheses = list_syntheses(conn, session_id)
    if not syntheses:
        return None
    return build_document(conn, syntheses[-1])


# ============================================================================
# Conversation: message list with safe inline citation segments
# ============================================================================

@dataclass(frozen=True)
class CitationSegment:
    kind: str  # "text" | "citation"
    text: str = ""
    citation_number: int = 0
    href: str = "#"
    title: str = ""
    quality_label: str = ""


@dataclass(frozen=True)
class MessageView:
    id: int
    role: str  # "owner" | "assistant"
    status: str  # "pending" | "ready" | "failed"
    segments: tuple[CitationSegment, ...]
    created_at_label: str


def _build_citation_segments(
        content: str, citation_numbers_to_item: dict[int, EvidenceItem],
        t: Localizer) -> tuple[CitationSegment, ...]:
    """Splits `content` on the "[N]" markers beehive.research.conversation._render_reply_content
    writes, turning each marker into a link ONLY when N is one of this exact message's own
    persisted citation_numbers (from research_message_citations); any other bracketed number --
    invented, a stray typed "[3]" in an owner's own free-text question, or a marker whose
    Evidence Item could not be resolved -- is left as inert plain text, never linked and never
    escalated to an error state."""
    segments: list[CitationSegment] = []
    last_end = 0
    for match in _CITATION_MARKER_RE.finditer(content):
        number = int(match.group(1))
        item = citation_numbers_to_item.get(number)
        if item is None:
            continue
        if match.start() > last_end:
            segments.append(CitationSegment(kind="text", text=content[last_end:match.start()]))
        segments.append(CitationSegment(
            kind="citation", citation_number=number, href=safe_external_href(item.url),
            title=item.title, quality_label=quality_label(item.quality, t)))
        last_end = match.end()
    if last_end < len(content):
        segments.append(CitationSegment(kind="text", text=content[last_end:]))
    if not segments:
        segments.append(CitationSegment(kind="text", text=content))
    return tuple(segments)


def build_message_view(conn, message: ConversationMessage, t: Localizer) -> MessageView:
    if message.role is ConversationRole.OWNER or message.status.value != "ready":
        segments = (CitationSegment(kind="text", text=message.content),)
    else:
        citations = list_message_citations(conn, message.id)
        item_ids = [c.evidence_item_id for c in citations]
        items_by_id = get_evidence_items(conn, item_ids)
        by_number = {
            c.citation_number: items_by_id[c.evidence_item_id]
            for c in citations if c.evidence_item_id in items_by_id
        }
        segments = _build_citation_segments(message.content, by_number, t)
    return MessageView(
        id=message.id,
        role=message.role.value,
        status=message.status.value,
        segments=segments,
        created_at_label=host_local_time_label(message.created_at.isoformat()),
    )


@dataclass(frozen=True)
class ConversationTabView:
    messages: tuple[MessageView, ...]
    has_pending_request: bool
    can_submit: bool
    disabled_reason: str | None
    failure_message: str | None


def build_conversation_view(
        conn, session: ResearchSession, synthesis_document: ResearchSynthesisDocument | None,
        evidence_all_excluded: bool, t: Localizer) -> ConversationTabView:
    messages = tuple(build_message_view(conn, m, t) for m in list_messages(conn, session.id))
    active_request: ChatRequest | None = get_active_chat_request(conn, session.id)
    has_pending = active_request is not None and active_request.status in (
        ChatRequestStatus.PENDING, ChatRequestStatus.PROCESSING)
    requests = list_chat_requests(conn, session.id)
    latest_request = requests[-1] if requests else None
    failure_message = (
        t.text("web.research.conversation.reply_failed")
        if latest_request is not None and latest_request.status is ChatRequestStatus.FAILED
        else None
    )

    latest_revision = get_latest_evidence_state_revision(conn, session.id)
    synthesis_is_current = (
        synthesis_document is not None and latest_revision is not None
        and synthesis_document.evidence_state_revision_id == latest_revision.id)

    disabled_reason = None
    can_submit = session.status is ResearchSessionStatus.ACTIVE
    if not can_submit:
        disabled_reason = t.text("web.research.conversation.disabled_archived")
    elif has_pending:
        can_submit = False
        disabled_reason = t.text("web.research.conversation.disabled_pending")
    elif synthesis_document is None:
        can_submit = False
        disabled_reason = t.text("web.research.conversation.disabled_no_synthesis")
    elif not synthesis_is_current:
        can_submit = False
        disabled_reason = t.text("web.research.conversation.disabled_synthesis_stale")
    elif evidence_all_excluded:
        can_submit = False
        disabled_reason = t.text("web.research.conversation.disabled_no_evidence")

    return ConversationTabView(
        messages=messages, has_pending_request=has_pending, can_submit=can_submit,
        disabled_reason=disabled_reason, failure_message=failure_message)


# ============================================================================
# Safe error-message mapping (never renders a raw ValueError/exception message)
# ============================================================================

_SESSION_ACTION_ERROR_KEY = "web.research.error.action_failed"


def safe_session_error_message(t: Localizer, _exc: Exception) -> str:
    """Every session-lifecycle mutation (archive/unarchive/refresh/cancel/delete/exclude/
    restore/message-submit) that can raise ValueError from a repository/domain call funnels its
    exception through this single function -- the exception's own message (which may legitimately
    reference internal ids) is intentionally discarded, never interpolated into the response."""
    return t.text(_SESSION_ACTION_ERROR_KEY)


@dataclass(frozen=True)
class SourceRowView:
    label: str


def build_source_rows(sources: list[ResearchSource], t: Localizer) -> tuple[SourceRowView, ...]:
    return tuple(SourceRowView(label=_connector_label(s.connector_type, s.config, t))
                 for s in sources)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
