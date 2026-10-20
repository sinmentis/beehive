"""Idempotent Owner setup for the single global PartyMap Civic Watch."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from beehive.civic.production import PARTY_ALIASES
from beehive.civic.scheduling import (
    DEFAULT_CIVIC_SCHEDULE_TIME,
    PARTYMAP_GLOBAL_WATCH_SUBJECT,
    PARTYMAP_GLOBAL_WATCH_TIMEZONE,
)
from beehive.db.civic_vocabulary import (
    _compute_content_hash,
    get_draft_vocabulary_version,
    get_published_vocabulary_version,
    list_vocabulary_terms,
    vocabulary_content_hash,
)
from beehive.db.civic_watches import (
    get_civic_watch_by_subject,
    list_schedulable_civic_watches,
)
from beehive.domain.civic import (
    CivicTermCategory,
    CivicTermRole,
    CivicWatch,
    CivicWatchStatus,
    normalize_term,
)

PARTYMAP_GLOBAL_WATCH_QUESTION = (
    "What durable civic changes involving a catalogued New Zealand political party were "
    "published for this Auckland day?"
)


class PartyMapSetupDisposition(StrEnum):
    CREATE_WATCH_AND_VOCABULARY = "create_watch_and_vocabulary"
    CREATE_VOCABULARY = "create_vocabulary"
    NOOP = "noop"


class PartyMapSetupConflictError(ValueError):
    pass


@dataclass(frozen=True)
class PartyMapSetupPlan:
    disposition: PartyMapSetupDisposition
    term_count: int
    actions: tuple[str, ...]
    watch_id: int | None = None


@dataclass(frozen=True)
class PartyMapSetupResult:
    plan: PartyMapSetupPlan
    applied: bool
    watch_id: int
    vocabulary_version_id: int | None


def required_party_terms() -> tuple[str, ...]:
    """Every closed alias once by the domain's persisted normalization rule."""
    by_normalized: dict[str, str] = {}
    for aliases in PARTY_ALIASES.values():
        for alias in aliases:
            by_normalized.setdefault(normalize_term(alias), alias)
    return tuple(by_normalized[key] for key in sorted(by_normalized))


def _expected_term_shape() -> set[tuple[str, CivicTermCategory, CivicTermRole]]:
    return {
        (normalize_term(term), CivicTermCategory.PARTY, CivicTermRole.REQUIRED)
        for term in required_party_terms()
    }


def _published_vocabulary_is_exact(conn: sqlite3.Connection, watch_id: int) -> bool:
    vocabulary = get_published_vocabulary_version(conn, watch_id)
    if vocabulary is None:
        return False
    actual = {
        (term.normalized_term, term.category, term.role)
        for term in list_vocabulary_terms(conn, vocabulary.id)
    }
    return (
        actual == _expected_term_shape()
        and vocabulary.content_hash == vocabulary_content_hash(conn, vocabulary.id)
    )


def plan_partymap_setup(conn: sqlite3.Connection) -> PartyMapSetupPlan:
    terms = required_party_terms()
    watch = get_civic_watch_by_subject(conn, PARTYMAP_GLOBAL_WATCH_SUBJECT)
    active = list_schedulable_civic_watches(conn)

    if watch is None:
        if active:
            raise PartyMapSetupConflictError(
                "cannot create the global PartyMap Watch while another Civic Watch is active")
        return PartyMapSetupPlan(
            disposition=PartyMapSetupDisposition.CREATE_WATCH_AND_VOCABULARY,
            term_count=len(terms),
            actions=(
                "create active Civic Watch 'partymap' at 06:30 Pacific/Auckland",
                f"publish a Civic Vocabulary with {len(terms)} required party aliases",
            ),
        )

    if watch.status is not CivicWatchStatus.ACTIVE:
        raise PartyMapSetupConflictError(
            f"existing 'partymap' Civic Watch is {watch.status.value}, not active")
    if (watch.schedule_timezone != PARTYMAP_GLOBAL_WATCH_TIMEZONE
            or watch.schedule_time != DEFAULT_CIVIC_SCHEDULE_TIME):
        raise PartyMapSetupConflictError(
            "existing 'partymap' Civic Watch is not scheduled for "
            "06:30 Pacific/Auckland")
    if len(active) != 1 or active[0].id != watch.id:
        raise PartyMapSetupConflictError(
            "the 'partymap' Civic Watch conflicts with another active Civic Watch")
    if get_draft_vocabulary_version(conn, watch.id) is not None:
        raise PartyMapSetupConflictError(
            "the 'partymap' Civic Watch has an Owner draft vocabulary; refusing to replace it")

    published = get_published_vocabulary_version(conn, watch.id)
    if published is None:
        return PartyMapSetupPlan(
            disposition=PartyMapSetupDisposition.CREATE_VOCABULARY,
            term_count=len(terms),
            actions=(
                f"publish a Civic Vocabulary with {len(terms)} required party aliases",
            ),
            watch_id=watch.id,
        )
    if not _published_vocabulary_is_exact(conn, watch.id):
        raise PartyMapSetupConflictError(
            "the published 'partymap' Civic Vocabulary differs from the checked-in party "
            "alias registry; refusing to replace Owner configuration")
    return PartyMapSetupPlan(
        disposition=PartyMapSetupDisposition.NOOP,
        term_count=len(terms),
        actions=(),
        watch_id=watch.id,
    )


def apply_partymap_setup(
    conn: sqlite3.Connection,
    now: datetime,
) -> PartyMapSetupResult:
    """Apply the plan atomically, or return the exact existing configuration unchanged."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("PartyMap Civic setup needs a timezone-aware datetime")
    conn.execute("BEGIN IMMEDIATE")
    try:
        plan = plan_partymap_setup(conn)
        if plan.disposition is PartyMapSetupDisposition.NOOP:
            result = PartyMapSetupResult(
                plan=plan,
                applied=False,
                watch_id=plan.watch_id,
                vocabulary_version_id=get_published_vocabulary_version(
                    conn, plan.watch_id).id,
            )
        else:
            watch_id = plan.watch_id
            if plan.disposition is PartyMapSetupDisposition.CREATE_WATCH_AND_VOCABULARY:
                CivicWatch(
                    id=None,
                    subject=PARTYMAP_GLOBAL_WATCH_SUBJECT,
                    question=PARTYMAP_GLOBAL_WATCH_QUESTION,
                    status=CivicWatchStatus.ACTIVE,
                    schedule_timezone=PARTYMAP_GLOBAL_WATCH_TIMEZONE,
                    schedule_time=DEFAULT_CIVIC_SCHEDULE_TIME,
                    created_at=now,
                )
                watch_id = conn.execute(
                    "INSERT INTO civic_watches (subject, question, status, schedule_timezone, "
                    "schedule_time, created_at) VALUES (?, ?, 'active', ?, ?, ?)",
                    (
                        PARTYMAP_GLOBAL_WATCH_SUBJECT,
                        PARTYMAP_GLOBAL_WATCH_QUESTION,
                        PARTYMAP_GLOBAL_WATCH_TIMEZONE,
                        DEFAULT_CIVIC_SCHEDULE_TIME,
                        now.isoformat(),
                    ),
                ).lastrowid

            current = conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS version "
                "FROM civic_vocabulary_versions WHERE watch_id = ?",
                (watch_id,),
            ).fetchone()["version"]
            vocabulary_id = conn.execute(
                "INSERT INTO civic_vocabulary_versions (watch_id, version, status, "
                "content_hash, created_at) VALUES (?, ?, 'draft', '', ?)",
                (watch_id, current + 1, now.isoformat()),
            ).lastrowid
            for term in required_party_terms():
                conn.execute(
                    "INSERT INTO civic_vocabulary_terms (vocabulary_version_id, term, "
                    "normalized_term, category, role, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        vocabulary_id,
                        term,
                        normalize_term(term),
                        CivicTermCategory.PARTY.value,
                        CivicTermRole.REQUIRED.value,
                        now.isoformat(),
                    ),
                )
            conn.execute(
                "UPDATE civic_vocabulary_versions SET status = 'published', "
                "content_hash = ?, published_at = ? WHERE id = ? AND status = 'draft'",
                (_compute_content_hash(conn, vocabulary_id), now.isoformat(), vocabulary_id),
            )
            result = PartyMapSetupResult(
                plan=plan,
                applied=True,
                watch_id=watch_id,
                vocabulary_version_id=vocabulary_id,
            )
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    return result


__all__ = [
    "PARTYMAP_GLOBAL_WATCH_QUESTION",
    "PartyMapSetupConflictError",
    "PartyMapSetupDisposition",
    "PartyMapSetupPlan",
    "PartyMapSetupResult",
    "apply_partymap_setup",
    "plan_partymap_setup",
    "required_party_terms",
]
