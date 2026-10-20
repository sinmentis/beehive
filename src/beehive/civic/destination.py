"""The production boundary between Beehive's broad policy and PartyMap publication v1.

The pure evidence engine intentionally understands more civic shapes than the first destination
contract. This module narrows only a decision that the broad engine would otherwise publish. An
evidence failure remains an evidence failure and may use its one research pass; a well-evidenced
shape the destination cannot represent is closed immediately as
``deferred_unsupported/destination_scope_v1`` and is never sent to AI.
"""
from __future__ import annotations

from dataclasses import replace

from beehive.civic.evidence import (
    CivicCandidateClaim,
    CivicClaimCategory,
    CivicPublicationDisposition,
    CivicQualification,
    CivicQualificationOutcome,
    CivicRecordKind,
    CivicRecordOperation,
)
from beehive.civic.reasons import CivicPolicyReason

DESTINATION_V1_ENGINE_VERSION = "civic-evidence/1+destination-scope-v1.2"
DESTINATION_V1_CATEGORIES = frozenset({
    CivicClaimCategory.PARTY_TIMELINE_EVENT,
    CivicClaimCategory.LEGISLATION,
    CivicClaimCategory.OFFICIAL_FINDING,
    CivicClaimCategory.ELECTORAL_ADMINISTRATION,
})


def is_destination_v1_claim(claim: CivicCandidateClaim) -> bool:
    """Whether the claim has an exact representation in civic-publication-v1."""
    return (
        claim.record_kind is CivicRecordKind.TIMELINE
        and claim.operation is CivicRecordOperation.ADD
        and claim.category in DESTINATION_V1_CATEGORIES
    )


def constrain_qualification_to_destination_v1(
    claim: CivicCandidateClaim,
    qualification: CivicQualification,
) -> CivicQualification:
    """Narrow a publishable broad-policy result to the exact v1 destination scope."""
    if not qualification.is_publishable or is_destination_v1_claim(claim):
        return qualification
    return replace(
        qualification,
        disposition=CivicPublicationDisposition.DEFERRED_UNSUPPORTED,
        outcome=CivicQualificationOutcome.DEFERRED_UNSUPPORTED,
        reasons=(CivicPolicyReason.DESTINATION_SCOPE_V1,),
    )


__all__ = [
    "DESTINATION_V1_CATEGORIES",
    "DESTINATION_V1_ENGINE_VERSION",
    "constrain_qualification_to_destination_v1",
    "is_destination_v1_claim",
]
