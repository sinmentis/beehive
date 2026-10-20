from __future__ import annotations

import json
from datetime import datetime, timezone

from beehive.civic.documents import CivicDocument
from beehive.civic.production import PARTY_ALIASES, filter_documents_for_vocabulary
from beehive.civic.setup import required_party_terms
from beehive.db.civic_vocabulary import (
    add_vocabulary_term,
    create_vocabulary_version,
    get_published_vocabulary_version,
    list_vocabulary_terms,
    list_vocabulary_versions,
    publish_vocabulary_version,
)
from beehive.db.civic_watches import (
    create_civic_watch,
    get_civic_watch_by_subject,
    list_civic_watches,
)
from beehive.db.connection import connect, init_schema
from beehive.domain.civic import (
    CivicEvidenceTier,
    CivicTermCategory,
    CivicTermRole,
    CivicWatchStatus,
    normalize_term,
)
from scripts.setup_partymap_civic import main

T0 = datetime(2026, 8, 27, 6, 30, tzinfo=timezone.utc)


def test_default_mode_plans_without_creating_owner_configuration(tmp_path, capsys):
    db_path = str(tmp_path / "plan.db")
    assert main(["--db-path", db_path]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["mode"] == "plan"
    assert output["disposition"] == "create_watch_and_vocabulary"
    assert not output["applied"]

    conn = connect(db_path)
    assert get_civic_watch_by_subject(conn, "partymap") is None
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM civic_vocabulary_versions").fetchone()["n"] == 0


def test_first_apply_creates_global_watch_and_closed_alias_vocabulary(tmp_path, capsys):
    db_path = str(tmp_path / "apply.db")
    assert main(["--db-path", db_path, "--apply"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["mode"] == "apply"
    assert output["applied"]

    conn = connect(db_path)
    watch = get_civic_watch_by_subject(conn, "partymap")
    assert watch.status is CivicWatchStatus.ACTIVE
    assert watch.schedule_timezone == "Pacific/Auckland"
    assert watch.schedule_time == "06:30"
    vocabulary = get_published_vocabulary_version(conn, watch.id)
    terms = list_vocabulary_terms(conn, vocabulary.id)
    expected = {normalize_term(alias) for aliases in PARTY_ALIASES.values() for alias in aliases}
    assert {term.normalized_term for term in terms} == expected
    assert normalize_term("TOP Party") not in expected
    assert normalize_term("Free Palestine") not in expected
    assert normalize_term("Opportunity Party") in expected
    assert normalize_term("Free Palestine Party") in expected
    assert len(terms) == len(expected) == output["term_count"]
    assert all(term.category is CivicTermCategory.PARTY for term in terms)
    assert all(term.role is CivicTermRole.REQUIRED for term in terms)

    for index, alias in enumerate(required_party_terms()):
        document = CivicDocument(
            source_key="nz_beehive_releases",
            external_key=str(index),
            canonical_url=f"https://www.beehive.govt.nz/release/{index}",
            title=f"{alias} announcement",
            tier=CivicEvidenceTier.OFFICIAL,
            publisher="New Zealand Government (Beehive)",
            first_seen_at=T0,
        )
        assert filter_documents_for_vocabulary([document], terms) == (document,)


def test_apply_replay_is_a_noop(tmp_path, capsys):
    db_path = str(tmp_path / "replay.db")
    assert main(["--db-path", db_path, "--apply"]) == 0
    capsys.readouterr()
    assert main(["--db-path", db_path, "--apply"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["disposition"] == "noop"
    assert not output["applied"]

    conn = connect(db_path)
    watch = get_civic_watch_by_subject(conn, "partymap")
    assert len(list_civic_watches(conn)) == 1
    assert len(list_vocabulary_versions(conn, watch.id)) == 1
    assert len(list_vocabulary_terms(
        conn, get_published_vocabulary_version(conn, watch.id).id
    )) == len(required_party_terms())


def test_conflicting_active_watch_fails_without_partial_setup(tmp_path, capsys):
    db_path = str(tmp_path / "active-conflict.db")
    conn = connect(db_path)
    init_schema(conn)
    create_civic_watch(conn, "national", "Owner configuration", T0)
    conn.close()

    assert main(["--db-path", db_path, "--apply"]) == 1
    output = json.loads(capsys.readouterr().err)
    assert output["status"] == "conflict"
    conn = connect(db_path)
    assert get_civic_watch_by_subject(conn, "partymap") is None
    assert len(list_civic_watches(conn)) == 1


def test_incompatible_owner_vocabulary_is_never_replaced(tmp_path, capsys):
    db_path = str(tmp_path / "vocabulary-conflict.db")
    conn = connect(db_path)
    init_schema(conn)
    watch = create_civic_watch(
        conn, "partymap", "Owner configuration", T0, schedule_time="06:30")
    vocabulary = create_vocabulary_version(conn, watch.id, T0)
    add_vocabulary_term(
        conn, vocabulary.id, "Owner term", CivicTermCategory.TOPIC,
        CivicTermRole.REQUIRED, T0)
    publish_vocabulary_version(conn, vocabulary.id, T0)
    conn.close()

    assert main(["--db-path", db_path, "--apply"]) == 1
    output = json.loads(capsys.readouterr().err)
    assert "refusing to replace" in output["error"]
    conn = connect(db_path)
    versions = list_vocabulary_versions(conn, watch.id)
    assert len(versions) == 1
    terms = list_vocabulary_terms(conn, versions[0].id)
    assert [(term.term, term.category, term.role) for term in terms] == [
        ("Owner term", CivicTermCategory.TOPIC, CivicTermRole.REQUIRED)]
