"""Civic Vocabulary persistence: the immutable, versioned term set a Civic Run screens against.

Two rules drive everything here, and both exist so a published statement stays explainable
forever:

* **A published version is frozen.** Terms may only be added to or removed from a DRAFT version.
  `publish_vocabulary_version` computes a content_hash over the version's own terms and, from
  that moment, every write path refuses to touch it. An already-delivered Civic Batch names the
  version it was screened against, so editing that version in place would retroactively change
  what a published statement was allowed to say.
* **Exactly one version is in force.** Publishing a new version supersedes the previous one in
  the same transaction, and the partial unique index on civic_vocabulary_versions(watch_id)
  WHERE status='published' is the DB-enforced backstop. "The vocabulary currently in force" is a
  single unambiguous row, never a set, so two concurrent publishes cannot both win.

The content_hash is a SHA-256 over the version's terms in a canonical order (normalized term,
then category, then role), so it is stable regardless of the order the terms were added in and
changes if any term, category, or role changes. It is the value a Civic Batch envelope quotes,
which is what lets a downstream consumer prove the vocabulary behind a publication has not been
edited underneath it."""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime

from beehive.db.connection import write_transaction
from beehive.domain.civic import (CivicTerm, CivicTermCategory, CivicTermRole,
                                   CivicVocabularyStatus, CivicVocabularyVersion,
                                   normalize_term, require_vocabulary_transition)


def _row_to_version(row: sqlite3.Row) -> CivicVocabularyVersion:
    return CivicVocabularyVersion(
        id=row["id"],
        watch_id=row["watch_id"],
        version=row["version"],
        status=CivicVocabularyStatus(row["status"]),
        content_hash=row["content_hash"],
        created_at=datetime.fromisoformat(row["created_at"]),
        published_at=(
            datetime.fromisoformat(row["published_at"]) if row["published_at"] else None),
        superseded_at=(
            datetime.fromisoformat(row["superseded_at"]) if row["superseded_at"] else None))


def _row_to_term(row: sqlite3.Row) -> CivicTerm:
    return CivicTerm(
        id=row["id"],
        vocabulary_version_id=row["vocabulary_version_id"],
        term=row["term"],
        normalized_term=row["normalized_term"],
        category=CivicTermCategory(row["category"]),
        role=CivicTermRole(row["role"]))


def _compute_content_hash(conn: sqlite3.Connection, version_id: int) -> str:
    """SHA-256 over the version's terms in canonical order. Order-independent by construction, so
    two versions holding the same terms hash identically however they were assembled."""
    rows = conn.execute(
        "SELECT normalized_term, category, role FROM civic_vocabulary_terms "
        "WHERE vocabulary_version_id = ? ORDER BY normalized_term, category, role",
        (version_id,)).fetchall()
    digest = hashlib.sha256()
    for row in rows:
        digest.update(f"{row['normalized_term']}\x1f{row['category']}\x1f{row['role']}\x1e"
                      .encode())
    return digest.hexdigest()


def _require_draft(conn: sqlite3.Connection, version_id: int) -> None:
    row = conn.execute(
        "SELECT status FROM civic_vocabulary_versions WHERE id = ?", (version_id,)).fetchone()
    if row is None:
        raise ValueError(f"no Civic Vocabulary version with id={version_id}")
    if row["status"] != CivicVocabularyStatus.DRAFT.value:
        raise ValueError(
            f"Civic Vocabulary version {version_id} is {row['status']!r}, not 'draft' -- a "
            "published vocabulary is immutable")


def create_vocabulary_version(
    conn: sqlite3.Connection, watch_id: int, now: datetime,
) -> CivicVocabularyVersion:
    """Starts a new DRAFT version for a Watch, numbered MAX(version)+1 under BEGIN IMMEDIATE so
    two concurrent drafts can never claim the same number. Raises ValueError if the Watch does
    not exist or already has a draft -- a Watch edits one draft at a time, so two Owners cannot
    each build a half-vocabulary that silently overwrites the other on publish."""
    with write_transaction(conn):
        if conn.execute(
                "SELECT 1 FROM civic_watches WHERE id = ?", (watch_id,)).fetchone() is None:
            raise ValueError(f"no Civic Watch with id={watch_id}")
        if conn.execute(
                "SELECT 1 FROM civic_vocabulary_versions WHERE watch_id = ? AND status = 'draft'",
                (watch_id,)).fetchone() is not None:
            raise ValueError(f"Civic Watch {watch_id} already has a draft Civic Vocabulary")
        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS current FROM civic_vocabulary_versions "
            "WHERE watch_id = ?", (watch_id,)).fetchone()
        cur = conn.execute(
            "INSERT INTO civic_vocabulary_versions (watch_id, version, status, content_hash, "
            "created_at) VALUES (?, ?, 'draft', '', ?)",
            (watch_id, row["current"] + 1, now.isoformat()))
        version_id = cur.lastrowid
    return get_vocabulary_version(conn, version_id)


def get_vocabulary_version(
    conn: sqlite3.Connection, version_id: int,
) -> CivicVocabularyVersion | None:
    row = conn.execute(
        "SELECT * FROM civic_vocabulary_versions WHERE id = ?", (version_id,)).fetchone()
    return _row_to_version(row) if row else None


def get_published_vocabulary_version(
    conn: sqlite3.Connection, watch_id: int,
) -> CivicVocabularyVersion | None:
    """The single version currently in force for a Watch, or None if nothing is published yet.
    A Civic Run pins this at claim time and never re-reads it, so a publish landing mid-run
    cannot change what that run is screening against."""
    row = conn.execute(
        "SELECT * FROM civic_vocabulary_versions WHERE watch_id = ? AND status = 'published'",
        (watch_id,)).fetchone()
    return _row_to_version(row) if row else None


def get_draft_vocabulary_version(
    conn: sqlite3.Connection, watch_id: int,
) -> CivicVocabularyVersion | None:
    row = conn.execute(
        "SELECT * FROM civic_vocabulary_versions WHERE watch_id = ? AND status = 'draft'",
        (watch_id,)).fetchone()
    return _row_to_version(row) if row else None


def list_vocabulary_versions(
    conn: sqlite3.Connection, watch_id: int,
) -> list[CivicVocabularyVersion]:
    rows = conn.execute(
        "SELECT * FROM civic_vocabulary_versions WHERE watch_id = ? ORDER BY version",
        (watch_id,)).fetchall()
    return [_row_to_version(row) for row in rows]


def add_vocabulary_term(
    conn: sqlite3.Connection,
    version_id: int,
    term: str,
    category: CivicTermCategory,
    role: CivicTermRole,
    now: datetime,
) -> CivicTerm:
    """Adds one term to a DRAFT version. The normalized form carries the uniqueness constraint,
    so "Green Party" and "green  party" cannot both be added; re-adding an existing normalized
    term raises ValueError rather than silently changing its role, because a role change is a
    meaningful edit an Owner has to make explicitly."""
    if not term.strip():
        raise ValueError("Civic Term must not be empty")
    normalized = normalize_term(term)
    with write_transaction(conn):
        _require_draft(conn, version_id)
        if conn.execute(
                "SELECT 1 FROM civic_vocabulary_terms WHERE vocabulary_version_id = ? "
                "AND normalized_term = ?", (version_id, normalized)).fetchone() is not None:
            raise ValueError(
                f"Civic Vocabulary version {version_id} already contains {normalized!r}")
        cur = conn.execute(
            "INSERT INTO civic_vocabulary_terms (vocabulary_version_id, term, normalized_term, "
            "category, role, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (version_id, term.strip(), normalized, category.value, role.value, now.isoformat()))
        term_id = cur.lastrowid
    row = conn.execute("SELECT * FROM civic_vocabulary_terms WHERE id = ?", (term_id,)).fetchone()
    return _row_to_term(row)


def remove_vocabulary_term(conn: sqlite3.Connection, version_id: int, term: str) -> bool:
    """Removes one term from a DRAFT version. Returns False if the term was not present."""
    normalized = normalize_term(term)
    with write_transaction(conn):
        _require_draft(conn, version_id)
        cur = conn.execute(
            "DELETE FROM civic_vocabulary_terms WHERE vocabulary_version_id = ? "
            "AND normalized_term = ?", (version_id, normalized))
        removed = cur.rowcount > 0
    return removed


def list_vocabulary_terms(conn: sqlite3.Connection, version_id: int) -> list[CivicTerm]:
    rows = conn.execute(
        "SELECT * FROM civic_vocabulary_terms WHERE vocabulary_version_id = ? "
        "ORDER BY normalized_term", (version_id,)).fetchall()
    return [_row_to_term(row) for row in rows]


def publish_vocabulary_version(
    conn: sqlite3.Connection, version_id: int, now: datetime,
) -> CivicVocabularyVersion:
    """Seals a DRAFT version and makes it the one in force, superseding the previous published
    version in the same transaction.

    Requires at least one REQUIRED term: a vocabulary with nothing required would admit every
    piece of material a connector ever returned, which is the opposite of what a controlled
    vocabulary is for. The content_hash is computed here, once, and never recomputed -- from this
    point the version is immutable, so a later hash mismatch means the row was tampered with,
    not that the vocabulary legitimately changed."""
    with write_transaction(conn):
        row = conn.execute(
            "SELECT watch_id, status FROM civic_vocabulary_versions WHERE id = ?",
            (version_id,)).fetchone()
        if row is None:
            raise ValueError(f"no Civic Vocabulary version with id={version_id}")
        require_vocabulary_transition(
            CivicVocabularyStatus(row["status"]), CivicVocabularyStatus.PUBLISHED)
        required = conn.execute(
            "SELECT COUNT(*) AS n FROM civic_vocabulary_terms "
            "WHERE vocabulary_version_id = ? AND role = 'required'", (version_id,)).fetchone()
        if required["n"] == 0:
            raise ValueError(
                "a published Civic Vocabulary needs at least one 'required' Civic Term")
        now_iso = now.isoformat()
        conn.execute(
            "UPDATE civic_vocabulary_versions SET status = 'superseded', superseded_at = ? "
            "WHERE watch_id = ? AND status = 'published'", (now_iso, row["watch_id"]))
        conn.execute(
            "UPDATE civic_vocabulary_versions SET status = 'published', content_hash = ?, "
            "published_at = ? WHERE id = ? AND status = 'draft'",
            (_compute_content_hash(conn, version_id), now_iso, version_id))
    return get_vocabulary_version(conn, version_id)


def vocabulary_content_hash(conn: sqlite3.Connection, version_id: int) -> str:
    """Recomputes the hash from the version's current terms -- the value a tamper check compares
    against the stored content_hash. Never used to update a published row."""
    return _compute_content_hash(conn, version_id)
