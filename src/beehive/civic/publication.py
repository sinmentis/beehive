"""The publication-v1 preflight and canonical wire format.

Only unsealed Candidates are evaluated against current content policy. A successful preflight
returns the actual item to freeze, not a second eligibility predicate. Sealed publications are
validated as artifacts and never rebuilt from current generation rules.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Mapping, Sequence

from jsonschema import Draft202012Validator, FormatChecker

from beehive.ai.model_selection import UnsupportedModelError
from beehive.ai.structured_response import StructuredResponseError
from beehive.civic.ai_gateway import (
    CIVIC_MODEL_PROVIDER,
    CivicAiStage,
    require_supported_model,
    verifier_model_for,
)
from beehive.civic.candidate_policy import CivicContentError, check_candidate_content
from beehive.civic.destination import DESTINATION_V1_ENGINE_VERSION
from beehive.civic.novelty import source_newness_issue
from beehive.civic.generation import (
    CIVIC_PROMPT_VERSION,
    CIVIC_SCHEMA_VERSION,
    CivicSnapshotError,
    build_candidate_snapshot,
    build_generation_prompt,
    build_verification_prompt,
    prompt_hash,
)
from beehive.connectors.civic_party import PARTY_CATALOG
from beehive.db.civic_ai_stages import (
    CivicAiStageReceipt,
    get_stage_receipt,
    output_hash_for,
)
from beehive.db.civic_candidates import list_candidate_evidence
from beehive.db.civic_qualifications import get_latest_qualification
from beehive.db.civic_verifications import list_verifications
from beehive.domain.civic import CivicEvidence, CivicVerificationVerdict

SCHEMA_VERSION = 1
_SCHEMA = json.loads(
    (Path(__file__).with_name("schemas") / "civic-publication-v1.schema.json")
    .read_text(encoding="utf-8"))
_ENVELOPE_VALIDATOR = Draft202012Validator(_SCHEMA, format_checker=FormatChecker())
_ITEM_VALIDATOR = Draft202012Validator(
    {"$ref": "#/$defs/item", "$defs": _SCHEMA["$defs"]}, format_checker=FormatChecker())
_LOCALES_VALIDATOR = Draft202012Validator(
    {"$ref": "#/$defs/locales", "$defs": _SCHEMA["$defs"]}, format_checker=FormatChecker())


class CivicPublicationIntegrityError(ValueError):
    """A stored publication artifact is inconsistent or corrupt."""


class CivicPublicationCandidateError(ValueError):
    """An unsealed Candidate cannot be represented honestly by publication v1."""

    def __init__(self, candidate_id: int, reason: str, detail: str) -> None:
        self.candidate_id = candidate_id
        self.reason = reason
        self.detail = detail
        super().__init__(f"Civic Candidate {candidate_id}: {detail}")


class CivicPublicationRefreshRequired(CivicPublicationCandidateError):
    """Deployment/input drift requires a new decision, not a negative news judgment."""


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False).encode("utf-8")


def payload_hash_for(envelope: Mapping) -> str:
    projected = dict(envelope)
    batch = dict(projected["batch"])
    batch.pop("payload_hash", None)
    projected["batch"] = batch
    return hashlib.sha256(canonical_json_bytes(projected)).hexdigest()


def _validate(validator: Draft202012Validator, value: object, label: str) -> None:
    error = next(validator.iter_errors(value), None)
    if error is not None:
        path = ".".join(str(part) for part in error.absolute_path) or "<root>"
        raise CivicPublicationIntegrityError(f"{label} {path}: {error.message}")


def validate_publication_envelope(envelope: object) -> None:
    _validate(_ENVELOPE_VALIDATOR, envelope, "publication envelope")


def validate_publication_item(item: object) -> None:
    _validate(_ITEM_VALIDATOR, item, "publication item")


def _validate_stored_locale_texts(output: object) -> None:
    if not isinstance(output, dict) or not isinstance(output.get("entry"), dict):
        raise CivicPublicationIntegrityError("publication locales have no recorded entry")
    locales = {
        locale: (
            {key: content[key] for key in ("title", "summary") if key in content}
            if isinstance(content, dict) else content)
        for locale, content in output["entry"].items()}
    _validate(_LOCALES_VALIDATOR, locales, "publication locales")


def _checked_stage_output(receipt: CivicAiStageReceipt) -> object:
    for field, stored, expected in (
        ("prompt_version", receipt.prompt_version, CIVIC_PROMPT_VERSION),
        ("schema_version", receipt.schema_version, CIVIC_SCHEMA_VERSION),
    ):
        if stored != expected:
            raise CivicPublicationRefreshRequired(
                receipt.candidate_id, "publication_stage_version_changed",
                f"{receipt.stage.value} receipt {field} is {stored!r}; "
                f"publication requires {expected!r}")
    if receipt.provider != CIVIC_MODEL_PROVIDER:
        raise CivicPublicationRefreshRequired(
            receipt.candidate_id, "publication_stage_provider_changed",
            f"{receipt.stage.value} receipt provider is {receipt.provider!r}; "
            f"publication requires {CIVIC_MODEL_PROVIDER!r}")
    try:
        require_supported_model(receipt.model)
    except UnsupportedModelError as exc:
        raise CivicPublicationRefreshRequired(
            receipt.candidate_id, "publication_stage_model_unsupported",
            f"{receipt.stage.value} receipt model is no longer supported: {receipt.model!r}"
        ) from exc
    try:
        output = receipt.output
    except json.JSONDecodeError as exc:
        raise CivicPublicationIntegrityError(
            f"Civic Candidate {receipt.candidate_id} {receipt.stage.value} receipt "
            "is not JSON") from exc
    if output_hash_for(output) != receipt.output_hash:
        raise CivicPublicationIntegrityError(
            f"Civic Candidate {receipt.candidate_id} {receipt.stage.value} receipt "
            "output_hash changed")
    return output


def publication_stamp(value: datetime, label: str) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise CivicPublicationIntegrityError(f"{label} must be timezone-aware")
    return value.isoformat()


def _citation(evidence: CivicEvidence, alias: str, candidate_id: int) -> dict:
    if (not evidence.accessible_full_text or evidence.paywalled
            or evidence.anonymous_source_dependency or evidence.discovery_only):
        raise CivicPublicationCandidateError(
            candidate_id, "publication_citation_invalid",
            f"chosen citation {alias!r} is not reader-checkable")
    return {
        "alias": alias,
        "document_key": evidence.external_key,
        "canonical_url": evidence.canonical_url,
        "publisher_id": evidence.publisher_id,
        "publisher": evidence.publisher,
        "source_kind": evidence.source_kind,
        "title": evidence.title,
        "published_at": (
            publication_stamp(evidence.published_at, "citation published_at")
            if evidence.published_at is not None else None),
        "captured_at": publication_stamp(evidence.captured_at, "citation captured_at"),
        "archive_url": None,
        "accessible_full_text": True,
        "paywalled": False,
        "anonymous_source_dependency": False,
    }


def preflight_candidate(conn: sqlite3.Connection, candidate_id: int) -> dict:
    """Validate current qualification, generated locales and their exact cited evidence.

    The generation input hash is essential: removing an earlier alias must reject a carryover,
    never silently rebind its ``e1`` to a different source. This function is read-only and makes
    no model calls, so seal can run it inside its membership transaction.
    """
    from beehive.civic.ai_pipeline import require_publishable_localization
    from beehive.db.civic_evidence_captures import require_current_source_capture

    require_current_source_capture(conn, candidate_id)
    qualification = get_latest_qualification(conn, candidate_id)
    if qualification is None:
        raise CivicPublicationRefreshRequired(
            candidate_id, "publication_qualification_missing", "no recorded qualification")
    if not qualification.is_publishable:
        raise CivicPublicationCandidateError(
            candidate_id, "publication_qualification_missing",
            "no publishable qualification")
    if qualification.engine_version != DESTINATION_V1_ENGINE_VERSION:
        raise CivicPublicationRefreshRequired(
            candidate_id, "publication_destination_scope",
            "qualification was not made under the current destination scope")
    try:
        snapshot = build_candidate_snapshot(
            conn, candidate_id=candidate_id, qualification=qualification)
    except CivicSnapshotError as exc:
        raise CivicPublicationCandidateError(
            candidate_id, "publication_snapshot_unusable", str(exc)) from exc
    if snapshot.subject_id not in PARTY_CATALOG.keys:
        raise CivicPublicationCandidateError(
            candidate_id, "publication_destination_scope", "subject is not a catalogued party")

    receipt = get_stage_receipt(conn, candidate_id, CivicAiStage.GENERATE)
    if receipt is None:
        raise CivicPublicationRefreshRequired(
            candidate_id, "publication_localization_missing", "no successful generation receipt")
    output = _checked_stage_output(receipt)
    if receipt.input_hash != snapshot.snapshot_hash:
        raise CivicPublicationRefreshRequired(
            candidate_id, "publication_snapshot_changed",
            "current evidence/aliases do not match the input that generated these locales")
    if receipt.prompt_hash != prompt_hash(build_generation_prompt(snapshot)):
        raise CivicPublicationRefreshRequired(
            candidate_id, "publication_generation_changed",
            "the recorded generator request does not match the current evidence and prompt")
    try:
        _validate_stored_locale_texts(output)
    except CivicPublicationIntegrityError as exc:
        raise CivicPublicationCandidateError(
            candidate_id, "publication_schema_invalid", str(exc)) from exc
    try:
        contents = require_publishable_localization(conn, candidate_id)
        ordered = check_candidate_content(
            snapshot, {content.locale: content for content in contents})
    except CivicContentError as exc:
        raise CivicPublicationCandidateError(
            candidate_id, f"publication_content_{exc.issue.value}", exc.detail) from exc
    except StructuredResponseError as exc:
        raise CivicPublicationCandidateError(
            candidate_id, "publication_localization_invalid", str(exc)) from exc

    aliases = ordered[0].citations
    if any(content.citations != aliases for content in ordered[1:]):
        raise CivicPublicationCandidateError(
            candidate_id, "publication_citation_mismatch", "locale citation lists differ")
    by_key = {
        evidence.external_key: evidence
        for evidence in list_candidate_evidence(conn, candidate_id)}
    citations = []
    for alias in aliases:
        selected = snapshot.alias_map.get(alias)
        if selected is None or selected.evidence_id not in by_key:
            raise CivicPublicationCandidateError(
                candidate_id, "publication_citation_invalid",
                f"chosen citation {alias!r} has no admissible persisted evidence")
        citations.append(_citation(by_key[selected.evidence_id], alias, candidate_id))
    item = {
        "record_key": snapshot.record_key,
        "operation": snapshot.operation,
        "record_kind": snapshot.record_kind,
        "subject": {"kind": "political-party", "id": snapshot.subject_id},
        "event_date": snapshot.event_date.isoformat(),
        "category": snapshot.category,
        "uncertainty": ordered[0].uncertainty,
        "locales": {
            content.locale.value: {"title": content.title, "summary": content.summary}
            for content in ordered},
        "citations": citations,
    }
    try:
        validate_publication_item(item)
    except CivicPublicationIntegrityError as exc:
        raise CivicPublicationCandidateError(
            candidate_id, "publication_schema_invalid", str(exc)) from exc
    verification = get_stage_receipt(conn, candidate_id, CivicAiStage.VERIFICATION)
    if verification is None:
        raise CivicPublicationRefreshRequired(
            candidate_id, "publication_verification_missing",
            "no successful independent verification receipt")
    report = _checked_stage_output(verification)
    try:
        expected_verifier = verifier_model_for(receipt.model)
    except UnsupportedModelError as exc:
        raise CivicPublicationRefreshRequired(
            candidate_id, "publication_verifier_policy_changed",
            "the stored drafting model cannot select a pinned independent verifier") from exc
    if verification.model != expected_verifier:
        raise CivicPublicationRefreshRequired(
            candidate_id, "publication_verifier_policy_changed",
            f"stored verifier {verification.model!r} does not match the current "
            f"independent verifier policy for {receipt.model!r}: {expected_verifier!r}")
    if report != {"verdict": "pass", "issues": []}:
        raise CivicPublicationCandidateError(
            candidate_id, "publication_verification_invalid",
            "independent verification did not pass")
    verification_prompt = build_verification_prompt(
        snapshot, {content.locale: content for content in ordered})
    if (verification.input_hash != snapshot.snapshot_hash
            or verification.prompt_hash != prompt_hash(verification_prompt)):
        raise CivicPublicationRefreshRequired(
            candidate_id, "publication_verification_changed",
            "the recorded verifier did not approve this exact entry and evidence input")
    # A later transport ERROR is not a contrary judgment. Preserve the repository's supporting
    # verdict semantics while requiring a judgment from the model whose bound stage passed.
    if not any(
        judgment.verdict is CivicVerificationVerdict.SUPPORTED
        and judgment.model == verification.model
        for judgment in list_verifications(conn, candidate_id)
    ):
        raise CivicPublicationRefreshRequired(
            candidate_id, "publication_verification_changed",
            "no recorded supporting judgment matches the successful verifier stage model")
    newness = source_newness_issue(
        title=item["locales"]["en"]["title"], summary=item["locales"]["en"]["summary"],
        subject_id=item["subject"]["id"],
        source_texts=tuple(
            f"{snapshot.alias_map[alias].title}\n{snapshot.alias_map[alias].excerpt}"
            for alias in aliases))
    if newness is not None:
        raise CivicPublicationCandidateError(
            candidate_id, "publication_unsupported_newness", newness)
    return item


def freeze_publication_envelope(
    *,
    idempotency_key: str,
    content_hash: str,
    slot_at: datetime,
    sealed_at: datetime,
    items: Sequence[dict],
) -> bytes:
    """Serialize only preflighted items, using the same authoritative wire validator."""
    envelope = {
        "schema_version": SCHEMA_VERSION,
        "batch": {
            "idempotency_key": idempotency_key,
            "batch_content_hash": content_hash,
            "slot_at": publication_stamp(slot_at, "batch slot_at"),
            "sealed_at": publication_stamp(sealed_at, "batch sealed_at"),
        },
        "items": list(items),
    }
    envelope["batch"]["payload_hash"] = payload_hash_for(envelope)
    validate_publication_envelope(envelope)
    return canonical_json_bytes(envelope)


def read_publication_envelope(data: bytes) -> dict:
    """Validate frozen bytes without re-evaluating any Candidate or generation policy."""
    try:
        envelope = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CivicPublicationIntegrityError("publication artifact is not UTF-8 JSON") from exc
    validate_publication_envelope(envelope)
    if canonical_json_bytes(envelope) != data:
        raise CivicPublicationIntegrityError("publication artifact is not canonical JSON")
    if payload_hash_for(envelope) != envelope["batch"]["payload_hash"]:
        raise CivicPublicationIntegrityError("publication artifact payload_hash does not match")
    return envelope
