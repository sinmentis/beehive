# tests/db/test_civic_vocabulary.py
"""Civic Vocabulary persistence: draft-only editing, the single published version in force, and
the content hash that makes a published vocabulary tamper-evident."""
from datetime import datetime, timedelta, timezone

import pytest

from beehive.db.civic_vocabulary import (add_vocabulary_term, create_vocabulary_version,
                                          get_draft_vocabulary_version,
                                          get_published_vocabulary_version,
                                          get_vocabulary_version, list_vocabulary_terms,
                                          list_vocabulary_versions, publish_vocabulary_version,
                                          remove_vocabulary_term, vocabulary_content_hash)
from beehive.db.civic_watches import create_civic_watch
from beehive.db.connection import connect, init_schema
from beehive.domain.civic import CivicTermCategory, CivicTermRole, CivicVocabularyStatus

T0 = datetime(2026, 8, 1, tzinfo=timezone.utc)
T1 = T0 + timedelta(hours=1)


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "test.db"))
    init_schema(c)
    return c


@pytest.fixture
def watch_id(conn):
    return create_civic_watch(conn, "NZ election", "Q", T0).id


def _draft_with_required_term(conn, watch_id, term="Green Party"):
    version = create_vocabulary_version(conn, watch_id, T0)
    add_vocabulary_term(
        conn, version.id, term, CivicTermCategory.PARTY, CivicTermRole.REQUIRED, T0)
    return version


# ============================================================================
# Version allocation
# ============================================================================


def test_first_version_is_number_one_and_starts_as_a_draft(conn, watch_id):
    version = create_vocabulary_version(conn, watch_id, T0)
    assert version.version == 1
    assert version.status is CivicVocabularyStatus.DRAFT
    assert version.content_hash == ""
    assert version.published_at is None


def test_a_watch_edits_one_draft_at_a_time(conn, watch_id):
    create_vocabulary_version(conn, watch_id, T0)
    with pytest.raises(ValueError, match="already has a draft"):
        create_vocabulary_version(conn, watch_id, T1)


def test_versions_are_numbered_sequentially_per_watch(conn, watch_id):
    first = _draft_with_required_term(conn, watch_id)
    publish_vocabulary_version(conn, first.id, T0)
    second = create_vocabulary_version(conn, watch_id, T1)
    assert second.version == 2
    assert [v.version for v in list_vocabulary_versions(conn, watch_id)] == [1, 2]


def test_creating_a_version_for_a_missing_watch_raises(conn):
    with pytest.raises(ValueError, match="no Civic Watch"):
        create_vocabulary_version(conn, 999, T0)


# ============================================================================
# Term editing
# ============================================================================


def test_terms_are_normalized_and_unique_within_a_version(conn, watch_id):
    version = create_vocabulary_version(conn, watch_id, T0)
    term = add_vocabulary_term(
        conn, version.id, "  Green   PARTY ", CivicTermCategory.PARTY, CivicTermRole.REQUIRED,
        T0)
    assert term.normalized_term == "green party"
    with pytest.raises(ValueError, match="already contains"):
        add_vocabulary_term(
            conn, version.id, "green party", CivicTermCategory.PARTY, CivicTermRole.EXCLUDED, T0)


def test_the_same_term_may_exist_in_two_different_versions(conn, watch_id):
    first = _draft_with_required_term(conn, watch_id)
    publish_vocabulary_version(conn, first.id, T0)
    second = create_vocabulary_version(conn, watch_id, T1)
    add_vocabulary_term(
        conn, second.id, "Green Party", CivicTermCategory.PARTY, CivicTermRole.REQUIRED, T1)
    assert len(list_vocabulary_terms(conn, second.id)) == 1


def test_empty_terms_are_rejected(conn, watch_id):
    version = create_vocabulary_version(conn, watch_id, T0)
    with pytest.raises(ValueError, match="must not be empty"):
        add_vocabulary_term(
            conn, version.id, "   ", CivicTermCategory.TOPIC, CivicTermRole.OPTIONAL, T0)


def test_removing_a_term_is_idempotent_on_a_draft(conn, watch_id):
    version = _draft_with_required_term(conn, watch_id)
    assert remove_vocabulary_term(conn, version.id, "GREEN party")
    assert not remove_vocabulary_term(conn, version.id, "Green Party")
    assert list_vocabulary_terms(conn, version.id) == []


# ============================================================================
# Publication and immutability
# ============================================================================


def test_publishing_seals_the_version_with_a_content_hash(conn, watch_id):
    version = _draft_with_required_term(conn, watch_id)
    published = publish_vocabulary_version(conn, version.id, T1)
    assert published.status is CivicVocabularyStatus.PUBLISHED
    assert published.published_at == T1
    assert published.content_hash == vocabulary_content_hash(conn, version.id)
    assert len(published.content_hash) == 64


def test_a_vocabulary_with_no_required_term_cannot_be_published(conn, watch_id):
    version = create_vocabulary_version(conn, watch_id, T0)
    add_vocabulary_term(
        conn, version.id, "rates", CivicTermCategory.TOPIC, CivicTermRole.OPTIONAL, T0)
    with pytest.raises(ValueError, match="at least one 'required'"):
        publish_vocabulary_version(conn, version.id, T1)
    assert get_vocabulary_version(conn, version.id).status is CivicVocabularyStatus.DRAFT


def test_an_empty_vocabulary_cannot_be_published(conn, watch_id):
    version = create_vocabulary_version(conn, watch_id, T0)
    with pytest.raises(ValueError, match="at least one 'required'"):
        publish_vocabulary_version(conn, version.id, T1)


def test_a_published_version_is_immutable(conn, watch_id):
    version = _draft_with_required_term(conn, watch_id)
    publish_vocabulary_version(conn, version.id, T1)
    with pytest.raises(ValueError, match="immutable"):
        add_vocabulary_term(
            conn, version.id, "Labour", CivicTermCategory.PARTY, CivicTermRole.REQUIRED, T1)
    with pytest.raises(ValueError, match="immutable"):
        remove_vocabulary_term(conn, version.id, "Green Party")


def test_republishing_a_published_version_is_refused(conn, watch_id):
    version = _draft_with_required_term(conn, watch_id)
    publish_vocabulary_version(conn, version.id, T1)
    with pytest.raises(ValueError, match="invalid Civic Vocabulary transition"):
        publish_vocabulary_version(conn, version.id, T1)


def test_publishing_supersedes_the_previous_version_in_the_same_step(conn, watch_id):
    first = _draft_with_required_term(conn, watch_id)
    publish_vocabulary_version(conn, first.id, T0)
    second = create_vocabulary_version(conn, watch_id, T1)
    add_vocabulary_term(
        conn, second.id, "Labour", CivicTermCategory.PARTY, CivicTermRole.REQUIRED, T1)
    publish_vocabulary_version(conn, second.id, T1)

    assert get_vocabulary_version(conn, first.id).status is CivicVocabularyStatus.SUPERSEDED
    assert get_vocabulary_version(conn, first.id).superseded_at == T1
    assert get_published_vocabulary_version(conn, watch_id).id == second.id


def test_only_one_version_is_ever_in_force(conn, watch_id):
    assert get_published_vocabulary_version(conn, watch_id) is None
    for term in ("Green Party", "Labour", "National"):
        version = create_vocabulary_version(conn, watch_id, T0)
        add_vocabulary_term(
            conn, version.id, term, CivicTermCategory.PARTY, CivicTermRole.REQUIRED, T0)
        publish_vocabulary_version(conn, version.id, T0)
    published = conn.execute(
        "SELECT COUNT(*) AS n FROM civic_vocabulary_versions "
        "WHERE watch_id = ? AND status = 'published'", (watch_id,)).fetchone()
    assert published["n"] == 1


def test_a_superseded_version_keeps_its_terms_and_hash_forever(conn, watch_id):
    first = _draft_with_required_term(conn, watch_id)
    published_first = publish_vocabulary_version(conn, first.id, T0)
    second = create_vocabulary_version(conn, watch_id, T1)
    add_vocabulary_term(
        conn, second.id, "Labour", CivicTermCategory.PARTY, CivicTermRole.REQUIRED, T1)
    publish_vocabulary_version(conn, second.id, T1)

    superseded = get_vocabulary_version(conn, first.id)
    assert superseded.content_hash == published_first.content_hash
    assert [t.normalized_term for t in list_vocabulary_terms(conn, first.id)] == ["green party"]


# ============================================================================
# Content hash
# ============================================================================


def test_content_hash_is_order_independent(conn):
    watch_a = create_civic_watch(conn, "A", "Q", T0).id
    watch_b = create_civic_watch(conn, "B", "Q", T0).id
    version_a = create_vocabulary_version(conn, watch_a, T0)
    version_b = create_vocabulary_version(conn, watch_b, T0)
    for term in ("Green Party", "Labour"):
        add_vocabulary_term(
            conn, version_a.id, term, CivicTermCategory.PARTY, CivicTermRole.REQUIRED, T0)
    for term in ("Labour", "Green Party"):
        add_vocabulary_term(
            conn, version_b.id, term, CivicTermCategory.PARTY, CivicTermRole.REQUIRED, T0)

    assert vocabulary_content_hash(conn, version_a.id) == vocabulary_content_hash(
        conn, version_b.id)


def test_content_hash_changes_when_a_role_changes(conn, watch_id):
    version = create_vocabulary_version(conn, watch_id, T0)
    add_vocabulary_term(
        conn, version.id, "rates", CivicTermCategory.TOPIC, CivicTermRole.REQUIRED, T0)
    before = vocabulary_content_hash(conn, version.id)
    remove_vocabulary_term(conn, version.id, "rates")
    add_vocabulary_term(
        conn, version.id, "rates", CivicTermCategory.TOPIC, CivicTermRole.EXCLUDED, T0)
    assert vocabulary_content_hash(conn, version.id) != before


def test_recomputed_hash_detects_a_tampered_published_version(conn, watch_id):
    version = _draft_with_required_term(conn, watch_id)
    published = publish_vocabulary_version(conn, version.id, T1)
    # Bypass the repository entirely, as a corrupted or hand-edited database would.
    conn.execute(
        "INSERT INTO civic_vocabulary_terms (vocabulary_version_id, term, normalized_term, "
        "category, role, created_at) VALUES (?, 'Smuggled', 'smuggled', 'party', 'required', ?)",
        (version.id, T1.isoformat()))
    conn.commit()
    assert vocabulary_content_hash(conn, version.id) != published.content_hash


def test_get_draft_returns_only_the_draft(conn, watch_id):
    version = _draft_with_required_term(conn, watch_id)
    assert get_draft_vocabulary_version(conn, watch_id).id == version.id
    publish_vocabulary_version(conn, version.id, T1)
    assert get_draft_vocabulary_version(conn, watch_id) is None
