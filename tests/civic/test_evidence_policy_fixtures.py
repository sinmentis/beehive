# tests/civic/test_evidence_policy_fixtures.py
"""Loader and schema checks for the Civic evidence policy fixture corpus.

The corpus in `tests/fixtures/civic/evidence_policy` is the contract the evidence engine will be
built against, so it has to be trustworthy before that engine exists. These tests therefore use
the standard library only and import nothing from `beehive`: a fixture that only validates
against the code it is meant to constrain proves nothing.

Three kinds of check live here:

* **Structural.** Versioned schema, closed vocabularies, unique ids, deterministic ordering, and
  a file name that matches the case id, so the corpus reads and diffs the same way every time.
* **Internal consistency.** Expected ownership groups are re-derived from the publisher registry,
  the used and excluded evidence sets partition the case, dedup windows agree with the timestamps
  in the case, and a retry case's final state is exactly its `after` state.
* **Coverage.** Every closed reason code is exercised, and every required policy branch has at
  least one positive and one negative case, which is what stops a branch quietly losing its
  counter-example.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "civic" / "evidence_policy"
SCHEMA = json.loads((FIXTURE_ROOT / "schema.json").read_text(encoding="utf-8"))
REGISTRY = json.loads((FIXTURE_ROOT / "publishers.json").read_text(encoding="utf-8"))
PUBLISHERS = {entry["publisherId"]: entry for entry in REGISTRY["publishers"]}

SCHEMA_VERSION = SCHEMA["fixtureSchemaVersion"]
VOCABULARIES = SCHEMA["vocabularies"]
REASON_CODES = SCHEMA["reasonCodes"]
REQUIRED_BRANCHES = SCHEMA["requiredBranches"]
OUTCOME_BY_ELIGIBILITY = SCHEMA["outcomeByEligibility"]
CATEGORY_RULES = SCHEMA["categoryEvidenceRules"]
MEDIA_ONLY_BRANCHES = set(SCHEMA["mediaOnlyBranches"])
PRECEDENCE_CODES = set(SCHEMA["precedenceReasonCodes"])
ADMISSIBILITY = SCHEMA["admissibilityRules"]
DISCOVERY_ONLY_CODE = ADMISSIBILITY["discoveryOnlyReasonCode"]
SINGLE_OWNERSHIP_CODE = ADMISSIBILITY["singleOwnershipReasonCode"]
MEDIA_INSUFFICIENCY_CODES = set(ADMISSIBILITY["mediaInsufficiencyCodes"])
TIER_RULE_CASES = SCHEMA["tierRuleCases"]
SEALED_WINDOW = timedelta(days=SCHEMA["dedupWindows"]["sealedCandidateDays"])
PUBLISHED_WINDOW = timedelta(days=SCHEMA["dedupWindows"]["publishedFingerprintDays"])
MAX_RESEARCH_ATTEMPTS = SCHEMA["researchRetry"]["maxAttemptsPerCandidate"]
URL_POLICY = SCHEMA["urlPolicy"]
TEXT_POLICY = SCHEMA["textPolicy"]

URL_PATTERN = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s\"'<>]+")
EMAIL_PATTERN = re.compile(r"[^\s@]+@[^\s@]+\.[a-zA-Z]{2,}")
PHONE_PATTERN = re.compile(r"(?:\+\d[\d\s-]{6,})|(?:\b\d{3}[\s-]\d{3,4}[\s-]\d{3,4}\b)")
HUMAN_TEXT_FIELDS = ("title", "description", "excerpt", "claimText", "name", "summary", "note")

# The scenarios the corpus must always contain. Branch coverage alone would let one of these be
# deleted as long as some other case kept its branch alive, and each of these is a policy
# decision someone deliberately made.
REQUIRED_CASE_IDS = (
    "official-electoral-singleton-valid",
    "official-parliament-singleton-valid",
    "official-legislation-singleton-valid",
    "party-policy-party-primary-valid",
    "party-statement-about-other-party-invalid",
    "official-finding-party-only-invalid",
    "media-two-ownership-groups-valid",
    "media-same-owner-duplicate-invalid",
    "media-crown-cluster-pair-invalid",
    "media-crown-cluster-plus-independent-valid",
    "media-unknown-owner-invalid",
    "discovery-aggregator-only-invalid",
    "discovery-community-forum-only-invalid",
    "discovery-social-post-only-invalid",
    "media-paywalled-without-corroboration-invalid",
    "media-anonymous-source-dependency-invalid",
    "media-no-accessible-full-article-invalid",
    "corroboration-subject-mismatch-invalid",
    "corroboration-event-date-mismatch-invalid",
    "corroboration-category-mismatch-invalid",
    "dedup-exact-canonical-duplicate-invalid",
    "dedup-sealed-candidate-within-seven-days-invalid",
    "dedup-sealed-candidate-at-seven-day-boundary-invalid",
    "dedup-sealed-candidate-just-outside-seven-days-valid",
    "dedup-published-fingerprint-within-fourteen-days-invalid",
    "dedup-published-fingerprint-at-fourteen-day-boundary-invalid",
    "dedup-published-fingerprint-just-outside-fourteen-days-valid",
    "research-retry-resolves-valid",
    "research-retry-exhausted-defers",
    "record-kind-unsupported-defers",
    "significance-poll-excluded",
    "significance-opinion-excluded",
    "significance-live-blog-excluded",
    "run-normal-batch-cap-overflow",
    "run-election-batch-cap-overflow",
)


def _load(directory: str) -> list[tuple[Path, dict]]:
    paths = sorted((FIXTURE_ROOT / directory).glob("*.json"))
    return [(path, json.loads(path.read_text(encoding="utf-8"))) for path in paths]


EVIDENCE_CASES = _load(SCHEMA["documents"]["evidence_case"]["directory"])
RUN_CASES = _load(SCHEMA["documents"]["run_case"]["directory"])
ALL_CASES = EVIDENCE_CASES + RUN_CASES

EVIDENCE_PARAMS = [pytest.param(doc, id=doc["id"]) for _, doc in EVIDENCE_CASES]
RUN_PARAMS = [pytest.param(doc, id=doc["id"]) for _, doc in RUN_CASES]
ALL_PARAMS = [pytest.param(path, doc, id=doc["id"]) for path, doc in ALL_CASES]


def _walk_strings(node: object, path: str = "") -> list[tuple[str, str]]:
    if isinstance(node, dict):
        found: list[tuple[str, str]] = []
        for key, value in node.items():
            found.extend(_walk_strings(value, f"{path}.{key}" if path else key))
        return found
    if isinstance(node, list):
        found = []
        for index, value in enumerate(node):
            found.extend(_walk_strings(value, f"{path}[{index}]"))
        return found
    if isinstance(node, str):
        return [(path, node)]
    return []


def _require_field_order(block: dict, order: list[str], required: list[str], label: str) -> None:
    keys = list(block)
    missing = [name for name in required if name not in keys]
    assert not missing, f"{label} is missing required fields {missing}"
    unknown = [name for name in keys if name not in order]
    assert not unknown, f"{label} carries unknown fields {unknown}"
    assert keys == [name for name in order if name in keys], (
        f"{label} keys {keys} are not in the schema order {order}")


def _sorted_unique(values: list[str], label: str) -> None:
    assert values == sorted(values), f"{label} must be sorted"
    assert len(values) == len(set(values)), f"{label} must not repeat a value"


def _media_entries(items: list[dict], used: list[str]) -> list[dict]:
    by_id = {item["evidenceId"]: item for item in items}
    return [
        PUBLISHERS[by_id[evidence_id]["publisherId"]] for evidence_id in used
        if PUBLISHERS[by_id[evidence_id]["publisherId"]]["kind"] == "media"
    ]


def _check_expected_block(expected: dict, items: list[dict], label: str) -> None:
    document = SCHEMA["documents"]["evidence_case"]
    _require_field_order(
        expected, document["expectedFieldOrder"], document["expectedRequired"], label)
    assert expected["eligibility"] in VOCABULARIES["eligibility"]
    assert expected["outcome"] in OUTCOME_BY_ELIGIBILITY[expected["eligibility"]], (
        f"{label} outcome {expected['outcome']} does not belong to eligibility "
        f"{expected['eligibility']}")

    codes = expected["reasonCodes"]
    _sorted_unique(codes, f"{label} reasonCodes")
    assert codes, f"{label} needs at least one reason code"
    unknown = [code for code in codes if code not in REASON_CODES]
    assert not unknown, f"{label} uses reason codes outside the closed vocabulary: {unknown}"
    dispositions = {REASON_CODES[code]["disposition"] for code in codes}
    if expected["eligibility"] == "publishable":
        assert dispositions == {"allow"}, (
            f"{label} is publishable but carries non allow codes {sorted(dispositions)}")
    elif expected["eligibility"] == "blocked":
        assert "block" in dispositions, f"{label} is blocked but carries no block code"
        assert "allow" not in dispositions, f"{label} is blocked but carries an allow code"
    else:
        assert "defer" in dispositions, f"{label} is deferred but carries no defer code"
        assert "block" not in dispositions, f"{label} is deferred but carries a block code"

    used, excluded = expected["evidenceUsed"], expected["evidenceExcluded"]
    _sorted_unique(used, f"{label} evidenceUsed")
    _sorted_unique(excluded, f"{label} evidenceExcluded")
    ids = sorted(item["evidenceId"] for item in items)
    assert sorted(used + excluded) == ids, (
        f"{label} evidenceUsed and evidenceExcluded must partition {ids}")

    by_id = {item["evidenceId"]: item for item in items}
    discovery = [
        evidence_id for evidence_id in used
        if by_id[evidence_id]["discoveryOnly"] or by_id[evidence_id]["sourceTier"] == "T5"]
    assert not discovery, f"{label} uses discovery tier evidence {discovery}"

    media = _media_entries(items, used)
    groups = sorted({entry["ownershipGroup"] for entry in media if entry["ownershipGroup"]})
    clusters = sorted({entry["ownershipCluster"] for entry in media if entry["ownershipCluster"]})
    assert expected["ownershipGroups"] == groups, (
        f"{label} ownershipGroups {expected['ownershipGroups']} do not match the registry "
        f"groups {groups} of its used media evidence")
    assert expected["ownershipClusters"] == clusters, (
        f"{label} ownershipClusters {expected['ownershipClusters']} do not match the registry "
        f"clusters {clusters} of its used media evidence")


def _all_items(doc: dict) -> list[dict]:
    return list(doc["evidence"]) + list(doc.get("retry", {}).get("additionalEvidence", []))


def _used_items(doc: dict, expected: dict | None = None) -> list[dict]:
    used = set((expected or doc["expected"])["evidenceUsed"])
    return [item for item in _all_items(doc) if item["evidenceId"] in used]


# --- Corpus level -------------------------------------------------------------------------------


def test_schema_and_registry_are_versioned_and_closed() -> None:
    assert SCHEMA_VERSION == 1
    assert REGISTRY["fixtureSchemaVersion"] == SCHEMA_VERSION
    assert set(SCHEMA["documents"]) == set(VOCABULARIES["kind"])
    for code, entry in REASON_CODES.items():
        assert entry["disposition"] in {"allow", "block", "defer"}, code
        assert entry["summary"].strip(), code
    assert list(REASON_CODES) == sorted(REASON_CODES), "reason codes must be sorted"
    assert list(REQUIRED_BRANCHES) == sorted(REQUIRED_BRANCHES), "branches must be sorted"


def test_corpus_is_not_empty_and_ids_are_unique() -> None:
    assert len(EVIDENCE_CASES) >= 30
    assert len(RUN_CASES) >= 4
    ids = [doc["id"] for _, doc in ALL_CASES]
    assert len(ids) == len(set(ids)), "fixture ids must be unique across the corpus"


def test_every_required_case_is_present() -> None:
    ids = {doc["id"] for _, doc in ALL_CASES}
    missing = sorted(set(REQUIRED_CASE_IDS) - ids)
    assert not missing, f"required policy cases are missing from the corpus: {missing}"


def test_case_load_order_is_deterministic() -> None:
    for directory in (SCHEMA["documents"][kind]["directory"] for kind in SCHEMA["documents"]):
        first = [doc["id"] for _, doc in _load(directory)]
        second = [doc["id"] for _, doc in _load(directory)]
        assert first == second == sorted(first)


def test_publisher_registry_is_internally_consistent() -> None:
    assert list(PUBLISHERS) == sorted(PUBLISHERS), "publishers must be sorted by id"
    for publisher_id, entry in PUBLISHERS.items():
        assert entry["kind"] in VOCABULARIES["publisherKind"], publisher_id
        assert entry["connector"] in VOCABULARIES["connector"], publisher_id
        assert entry["defaultSourceTier"] in VOCABULARIES["sourceTier"], publisher_id
        assert entry["host"].endswith(URL_POLICY["allowedHostSuffix"]), publisher_id
        if entry["kind"] == "discovery":
            assert entry["defaultSourceTier"] == "T5", publisher_id
            assert entry["sourceKind"] is None, publisher_id
            assert entry["ownershipGroup"] is None, publisher_id
        else:
            assert entry["sourceKind"] in VOCABULARIES["sourceKind"], publisher_id
        if entry["kind"] == "media":
            assert entry["defaultSourceTier"] == "T4", publisher_id
            assert (entry["ownershipGroup"] is None) == (not entry["registered"]), publisher_id
            assert (entry["ownershipGroup"] is None) == (
                entry["ownershipCluster"] is None), publisher_id
        else:
            assert entry["ownershipGroup"] is None, publisher_id
        if entry["kind"] == "party":
            assert entry["partySlug"], publisher_id


@pytest.mark.parametrize("branch", sorted(REQUIRED_BRANCHES))
def test_every_required_branch_has_a_positive_and_a_negative_case(branch: str) -> None:
    polarities = {
        doc["polarity"] for _, doc in ALL_CASES if branch in doc["branches"]}
    assert "positive" in polarities, f"branch {branch} has no positive case"
    assert "negative" in polarities, f"branch {branch} has no negative case"


@pytest.mark.parametrize("code", sorted(REASON_CODES))
def test_every_reason_code_is_exercised(code: str) -> None:
    for _, doc in ALL_CASES:
        blocks = [doc["expected"]]
        if "retry" in doc:
            blocks.extend([doc["retry"]["before"], doc["retry"]["after"]])
        if any(code in block["reasonCodes"] for block in blocks):
            return
    pytest.fail(f"reason code {code} is declared but never exercised by a fixture")


# --- Every document -----------------------------------------------------------------------------


@pytest.mark.parametrize(("path", "doc"), ALL_PARAMS)
def test_document_header_matches_its_file(path: Path, doc: dict) -> None:
    assert doc["fixtureSchemaVersion"] == SCHEMA_VERSION
    assert doc["kind"] in VOCABULARIES["kind"]
    assert doc["id"] == path.stem, "the file name must be the case id"
    assert path.parent.name == SCHEMA["documents"][doc["kind"]]["directory"]
    assert doc["polarity"] in VOCABULARIES["polarity"]
    assert doc["title"].strip() and doc["description"].strip()
    _sorted_unique(doc["branches"], f"{doc['id']} branches")
    unknown = [name for name in doc["branches"] if name not in REQUIRED_BRANCHES]
    assert not unknown, f"{doc['id']} names unknown branches {unknown}"


@pytest.mark.parametrize(("path", "doc"), ALL_PARAMS)
def test_document_urls_stay_inside_reserved_example_hosts(path: Path, doc: dict) -> None:
    for field, text in _walk_strings(doc):
        for match in URL_PATTERN.findall(text):
            scheme, _, remainder = match.partition("://")
            host = remainder.split("/")[0].split("?")[0].split("#")[0].split("@")[-1]
            host = host.split(":")[0]
            assert scheme == URL_POLICY["scheme"], f"{doc['id']}.{field} uses scheme {scheme}"
            assert host.endswith(URL_POLICY["allowedHostSuffix"]), (
                f"{doc['id']}.{field} points at {host}, which is not a reserved example host")


@pytest.mark.parametrize(("path", "doc"), ALL_PARAMS)
def test_document_text_is_synthetic_and_bounded(path: Path, doc: dict) -> None:
    for field, text in _walk_strings(doc):
        for forbidden in TEXT_POLICY["forbiddenCharacters"]:
            assert forbidden not in text, f"{doc['id']}.{field} contains a forbidden dash"
        leaf = field.split(".")[-1].split("[")[0]
        if leaf not in HUMAN_TEXT_FIELDS:
            continue
        assert not EMAIL_PATTERN.search(text), f"{doc['id']}.{field} looks like an email address"
        assert not PHONE_PATTERN.search(text), f"{doc['id']}.{field} looks like a phone number"
        if leaf == "excerpt":
            assert len(text) <= TEXT_POLICY["maxExcerptCharacters"], (
                f"{doc['id']}.{field} exceeds the excerpt budget")
        if leaf == "title":
            assert len(text) <= TEXT_POLICY["maxTitleCharacters"], (
                f"{doc['id']}.{field} exceeds the title budget")


# --- Evidence cases -----------------------------------------------------------------------------


@pytest.mark.parametrize("doc", EVIDENCE_PARAMS)
def test_evidence_case_field_order(doc: dict) -> None:
    document = SCHEMA["documents"]["evidence_case"]
    _require_field_order(doc, document["fieldOrder"], document["required"], doc["id"])
    _require_field_order(
        doc["candidate"], document["candidateFieldOrder"], document["candidateRequired"],
        f"{doc['id']}.candidate")
    for item in _all_items(doc):
        _require_field_order(
            item, document["evidenceFieldOrder"], document["evidenceRequired"],
            f"{doc['id']}.evidence[{item['evidenceId']}]")


@pytest.mark.parametrize("doc", EVIDENCE_PARAMS)
def test_evidence_case_candidate_uses_closed_vocabularies(doc: dict) -> None:
    candidate = doc["candidate"]
    assert candidate["recordKind"] in VOCABULARIES["recordKind"]
    assert candidate["operation"] in VOCABULARIES["operation"]
    assert candidate["category"] in VOCABULARIES["category"]
    assert candidate["contentClass"] in VOCABULARIES["contentClass"]
    assert isinstance(candidate["durable"], bool)
    datetime.strptime(candidate["eventDate"], "%Y-%m-%d")
    assert candidate["recordKey"].startswith(f"{candidate['subjectId']}/")
    supported = candidate["recordKind"] in VOCABULARIES["supportedRecordKind"]
    if not supported:
        assert doc["expected"]["outcome"] == "deferred_unsupported", (
            f"{doc['id']} uses an unsupported record kind but is not deferred as unsupported")


@pytest.mark.parametrize("doc", EVIDENCE_PARAMS)
def test_evidence_items_are_unique_ordered_and_registered(doc: dict) -> None:
    items = _all_items(doc)
    assert doc["evidence"], f"{doc['id']} needs at least one evidence item"
    ids = [item["evidenceId"] for item in doc["evidence"]]
    _sorted_unique(ids, f"{doc['id']} evidence ids")
    assert len({item["evidenceId"] for item in items}) == len(items), (
        f"{doc['id']} repeats an evidence id across its initial and additional evidence")
    for item in items:
        label = f"{doc['id']}.evidence[{item['evidenceId']}]"
        entry = PUBLISHERS.get(item["publisherId"])
        assert entry is not None, f"{label} names an unregistered publisher"
        assert item["connector"] == entry["connector"], label
        assert item["sourceTier"] == entry["defaultSourceTier"], label
        assert item["sourceKind"] == entry["sourceKind"], label
        assert item["civicEvidenceTier"] in VOCABULARIES["civicEvidenceTier"], label
        assert item["stance"] in VOCABULARIES["evidenceStance"], label
        assert item["category"] in VOCABULARIES["category"], label
        assert item["url"].startswith(f"https://{entry['host']}/"), label
        assert item["canonicalUrl"].startswith(f"https://{entry['host']}/"), label
        assert "?" not in item["canonicalUrl"] and "#" not in item["canonicalUrl"], (
            f"{label} canonicalUrl must be normalized")
        datetime.strptime(item["eventDate"], "%Y-%m-%d")
        datetime.fromisoformat(item["publishedAt"])
        assert item["discoveryOnly"] == (item["sourceTier"] == "T5"), label
        if item["discoveryOnly"]:
            assert item["civicEvidenceTier"] == "aggregator", label
        if item["civicEvidenceTier"] == "party_primary":
            assert item.get("partySlug"), f"{label} must name the party it speaks for"
            assert item["partySlug"] == entry["partySlug"], label
        if entry["kind"] == "media" and not entry["registered"]:
            assert item["civicEvidenceTier"] != "reputable_media", label
        if item["paywalled"]:
            assert not item["accessibleFullText"], f"{label} cannot be paywalled and accessible"


@pytest.mark.parametrize("doc", EVIDENCE_PARAMS)
def test_evidence_case_expectation_is_consistent(doc: dict) -> None:
    _check_expected_block(doc["expected"], _all_items(doc), f"{doc['id']}.expected")
    publishable = doc["expected"]["eligibility"] == "publishable"
    assert (doc["polarity"] == "positive") == publishable, (
        f"{doc['id']} polarity does not match its expected eligibility")


@pytest.mark.parametrize("doc", EVIDENCE_PARAMS)
def test_research_retry_is_bounded_and_terminal(doc: dict) -> None:
    retry = doc.get("retry")
    if retry is None:
        assert "research_retry_resolved" not in doc["expected"]["reasonCodes"]
        assert "research_retry_exhausted" not in doc["expected"]["reasonCodes"]
        return
    document = SCHEMA["documents"]["evidence_case"]
    _require_field_order(
        retry, document["retryFieldOrder"], document["retryRequired"], f"{doc['id']}.retry")
    assert retry["maxAttempts"] == MAX_RESEARCH_ATTEMPTS
    assert retry["attemptsUsed"] == retry["maxAttempts"]
    assert retry["additionalEvidence"], f"{doc['id']} retry added no evidence"
    _check_expected_block(retry["before"], doc["evidence"], f"{doc['id']}.retry.before")
    _check_expected_block(retry["after"], _all_items(doc), f"{doc['id']}.retry.after")
    assert retry["before"]["eligibility"] != "publishable", (
        f"{doc['id']} would not have needed a research attempt")
    assert retry["after"] == doc["expected"], (
        f"{doc['id']} expected state must be the state after its one research attempt")
    resolved = retry["after"]["eligibility"] == "publishable"
    marker = "research_retry_resolved" if resolved else "research_retry_exhausted"
    assert marker in retry["after"]["reasonCodes"], (
        f"{doc['id']} retry outcome is not recorded as {marker}")


@pytest.mark.parametrize("doc", EVIDENCE_PARAMS)
def test_dedup_context_matches_the_expected_dedup_codes(doc: dict) -> None:
    codes = set(doc["expected"]["reasonCodes"])
    dedup_codes = {code for code in codes if code.startswith(("dedup_", "duplicate_"))}
    context = doc.get("dedupContext")
    if context is None:
        assert not dedup_codes, f"{doc['id']} claims {sorted(dedup_codes)} without a dedupContext"
        return
    document = SCHEMA["documents"]["evidence_case"]
    _require_field_order(
        context, document["dedupContextFieldOrder"], document["dedupContextRequired"],
        f"{doc['id']}.dedupContext")
    now = datetime.fromisoformat(context["now"])
    fingerprint = doc["candidate"]["fingerprint"]
    canonical = {item["canonicalUrl"] for item in _all_items(doc)}

    sealed_matches = [
        (entry, now - datetime.fromisoformat(entry["sealedAt"]))
        for entry in context["priorSealedCandidates"] if entry["fingerprint"] == fingerprint]
    published_matches = [
        (entry, now - datetime.fromisoformat(entry["publishedAt"]))
        for entry in context["publishedFingerprints"] if entry["fingerprint"] == fingerprint]
    url_matches = [
        entry for entry in context["priorSealedCandidates"] if entry["canonicalUrl"] in canonical]

    if "duplicate_canonical_url" in codes:
        assert url_matches, f"{doc['id']} claims a canonical duplicate with no matching prior URL"
    if "duplicate_sealed_candidate_within_seven_days" in codes:
        assert any(timedelta() <= age <= SEALED_WINDOW for _, age in sealed_matches), (
            f"{doc['id']} claims a sealed duplicate with no match inside the seven day window")
    if "dedup_outside_sealed_window" in codes:
        assert sealed_matches, f"{doc['id']} claims an aged sealed match but has none"
        assert all(age > SEALED_WINDOW for _, age in sealed_matches), (
            f"{doc['id']} claims the sealed match is outside the window but it is not")
    if "duplicate_published_fingerprint_within_fourteen_days" in codes:
        assert any(timedelta() <= age <= PUBLISHED_WINDOW for _, age in published_matches), (
            f"{doc['id']} claims a published duplicate with no match inside the fourteen day "
            f"window")
    if "dedup_outside_published_window" in codes:
        assert published_matches, f"{doc['id']} claims an aged published match but has none"
        assert all(age > PUBLISHED_WINDOW for _, age in published_matches), (
            f"{doc['id']} claims the published match is outside the window but it is not")
    if "dedup_no_prior_match" in codes:
        assert not sealed_matches and not published_matches and not url_matches, (
            f"{doc['id']} claims no prior match but its dedupContext contains one")


# --- Category and tier consistency ----------------------------------------------------------------
#
# A fixture is vacuous when a stricter rule decides it before the rule it was written to
# exercise. A media ownership case categorised as `legislation`, for example, would defer on
# plan 4.2 rule 2 (a legal state needs a T0 source) no matter what its publishers were, so it
# would prove nothing about ownership. These tests pin each case to the rule it is meant to
# reach: `party-timeline-event` is the one category whose evidence can be media only, and the
# categories that name a stricter tier keep dedicated cases that prove exactly that.


def test_every_category_carries_an_evidence_rule() -> None:
    assert set(CATEGORY_RULES) == set(VOCABULARIES["category"])
    assert list(CATEGORY_RULES) == sorted(CATEGORY_RULES)
    for category, rule in CATEGORY_RULES.items():
        assert rule["rule"].strip(), category
        assert set(rule["sufficientTiers"]) <= set(VOCABULARIES["sourceTier"]), category
        assert "T5" not in rule["sufficientTiers"], category
        if not rule["publishable"]:
            assert rule["sufficientTiers"] == [], category
            assert not rule["mediaOnlyPublishable"], category
        assert rule["mediaOnlyPublishable"] == ("T4" in rule["sufficientTiers"]), (
            f"{category} may only be media only publishable when T4 is a sufficient tier")
        if rule["requiresMatchingParty"]:
            assert rule["sufficientTiers"] == ["T3"], category


def test_media_only_branches_and_precedence_codes_are_known() -> None:
    assert MEDIA_ONLY_BRANCHES <= set(REQUIRED_BRANCHES)
    assert PRECEDENCE_CODES <= set(REASON_CODES)
    assert all(REASON_CODES[code]["disposition"] != "allow" for code in PRECEDENCE_CODES)
    media_categories = [
        category for category, rule in CATEGORY_RULES.items() if rule["mediaOnlyPublishable"]]
    assert media_categories == ["party-timeline-event"], (
        "rule 4 media only evidence has exactly one category")


@pytest.mark.parametrize("doc", EVIDENCE_PARAMS)
def test_publishable_case_meets_its_category_evidence_rule(doc: dict) -> None:
    if doc["expected"]["eligibility"] != "publishable":
        return
    category = doc["candidate"]["category"]
    rule = CATEGORY_RULES[category]
    assert rule["publishable"], f"{doc['id']} publishes an excluded category {category}"
    used = _used_items(doc)
    tiers = {item["sourceTier"] for item in used}
    if not rule["mediaOnlyPublishable"]:
        qualifying = tiers & set(rule["sufficientTiers"])
        assert qualifying, (
            f"{doc['id']} publishes {category} on tiers {sorted(tiers)}, but {rule['rule']}")
    if rule["requiresMatchingParty"]:
        subject = doc["candidate"]["subjectId"]
        matching = [
            item for item in used
            if item["sourceTier"] == "T3" and item.get("partySlug") == subject]
        assert matching, (
            f"{doc['id']} publishes {category} without a party primary source for {subject}")


@pytest.mark.parametrize("doc", EVIDENCE_PARAMS)
def test_media_only_publishable_case_uses_the_party_timeline_category(doc: dict) -> None:
    if doc["expected"]["eligibility"] != "publishable":
        return
    tiers = {item["sourceTier"] for item in _used_items(doc)}
    if tiers <= {"T4"} and tiers:
        assert CATEGORY_RULES[doc["candidate"]["category"]]["mediaOnlyPublishable"], (
            f"{doc['id']} publishes on reputable media alone, which only plan 4.2 rule 4 allows, "
            f"so it must be a party timeline event")


@pytest.mark.parametrize("doc", EVIDENCE_PARAMS)
def test_media_branch_case_reaches_its_intended_rule(doc: dict) -> None:
    branches = set(doc["branches"]) & MEDIA_ONLY_BRANCHES
    if not branches:
        return
    category = doc["candidate"]["category"]
    assert CATEGORY_RULES[category]["mediaOnlyPublishable"], (
        f"{doc['id']} exercises {sorted(branches)} but its category {category} would be decided "
        f"by a stricter tier rule first")
    candidate = doc["candidate"]
    assert candidate["contentClass"] == "factual", doc["id"]
    assert candidate["durable"] is True, doc["id"]
    assert candidate["recordKind"] in VOCABULARIES["supportedRecordKind"], doc["id"]
    blocks = [doc["expected"]]
    if "retry" in doc:
        blocks.append(doc["retry"]["before"])
    for block in blocks:
        preempted = set(block["reasonCodes"]) & PRECEDENCE_CODES
        assert not preempted, (
            f"{doc['id']} exercises {sorted(branches)} but is decided by {sorted(preempted)}")


@pytest.mark.parametrize("case_id", sorted(TIER_RULE_CASES))
def test_tier_rule_case_keeps_proving_its_tier_rule(case_id: str) -> None:
    rule = TIER_RULE_CASES[case_id]
    docs = [doc for _, doc in EVIDENCE_CASES if doc["id"] == case_id]
    assert docs, f"tier rule case {case_id} is missing from the corpus"
    doc = docs[0]
    assert doc["candidate"]["category"] == rule["category"], (
        f"{case_id} must keep proving the {rule['category']} tier rule")
    tiers = {item["sourceTier"] for item in _used_items(doc)}
    if "requiredUsedTier" in rule:
        assert rule["requiredUsedTier"] in tiers, (
            f"{case_id} no longer carries {rule['requiredUsedTier']} evidence")
    if "usedTiersSubsetOf" in rule:
        assert tiers <= set(rule["usedTiersSubsetOf"]), (
            f"{case_id} must stay limited to {rule['usedTiersSubsetOf']} evidence to prove that "
            f"those tiers are not enough")
    assert rule["requiredReasonCode"] in doc["expected"]["reasonCodes"], (
        f"{case_id} no longer records {rule['requiredReasonCode']}")


def test_party_timeline_event_category_is_exercised_both_ways() -> None:
    polarities = {
        doc["polarity"] for _, doc in EVIDENCE_CASES
        if doc["candidate"]["category"] == "party-timeline-event"}
    assert polarities == {"positive", "negative"}


# --- Discovery admissibility ------------------------------------------------------------------------
#
# Discovery tier material is removed before the evidence rules run, so it never explains why a
# candidate that still holds admissible evidence failed. A case that keeps one registered
# publisher alongside a forum thread fails because one ownership group cannot carry a media only
# fact, not because a forum thread exists.


def test_admissibility_rules_name_known_reason_codes() -> None:
    assert DISCOVERY_ONLY_CODE in REASON_CODES
    assert SINGLE_OWNERSHIP_CODE in MEDIA_INSUFFICIENCY_CODES
    assert MEDIA_INSUFFICIENCY_CODES <= set(REASON_CODES)
    assert all(REASON_CODES[code]["disposition"] == "defer" for code in MEDIA_INSUFFICIENCY_CODES)
    assert ADMISSIBILITY["note"].strip()


@pytest.mark.parametrize("doc", EVIDENCE_PARAMS)
def test_discovery_only_code_requires_no_admissible_evidence(doc: dict) -> None:
    if DISCOVERY_ONLY_CODE not in doc["expected"]["reasonCodes"]:
        return
    assert doc["expected"]["evidenceUsed"] == [], (
        f"{doc['id']} records {DISCOVERY_ONLY_CODE} while still holding admissible evidence "
        f"{doc['expected']['evidenceUsed']}, so the discovery rule is not what decided it")


@pytest.mark.parametrize("doc", EVIDENCE_PARAMS)
def test_mixed_discovery_case_fails_on_a_media_rule(doc: dict) -> None:
    items = _all_items(doc)
    if not any(item["discoveryOnly"] for item in items):
        return
    used = _used_items(doc)
    if not used:
        return
    codes = set(doc["expected"]["reasonCodes"])
    assert DISCOVERY_ONLY_CODE not in codes, (
        f"{doc['id']} keeps admissible evidence, so discovery material cannot be its reason")
    excluded = {item["evidenceId"] for item in items if item["discoveryOnly"]}
    assert excluded <= set(doc["expected"]["evidenceExcluded"]), (
        f"{doc['id']} must keep its discovery items excluded")
    groups = {
        PUBLISHERS[item["publisherId"]]["ownershipGroup"] for item in used
        if PUBLISHERS[item["publisherId"]]["kind"] == "media"}
    groups.discard(None)
    if doc["expected"]["eligibility"] == "publishable":
        assert len(groups) >= 2, doc["id"]
        return
    assert codes & MEDIA_INSUFFICIENCY_CODES, (
        f"{doc['id']} holds admissible media evidence but records none of the media rules "
        f"{sorted(MEDIA_INSUFFICIENCY_CODES)}")
    if len(groups) == 1:
        assert SINGLE_OWNERSHIP_CODE in codes, (
            f"{doc['id']} has one admissible ownership group, so it must fail on "
            f"{SINGLE_OWNERSHIP_CODE}")


# --- Dedup window discrimination -------------------------------------------------------------------
#
# The seven day sealed candidate window and the fourteen day published fingerprint window are two
# different rules with an inclusive edge, so the corpus has to pin four things at once: which
# window applies to which dimension, the exact integer day count of each, and that a match landing
# exactly on an edge is a duplicate. Each dimension therefore carries four cases: comfortably
# inside, comfortably outside, exactly on the edge, and one minute past it. The simulation below
# is deliberately trivial arithmetic over the fixture timestamps rather than a policy engine: it
# only asks which window settings reproduce the corpus, and fails if anything other than the
# declared, inclusive pair does.

_MAX_SWEEP_DAYS = 30


def _window_decisions(
    sealed: timedelta, published: timedelta, *,
    sealed_inclusive: bool = True, published_inclusive: bool = True,
) -> dict[str, tuple[bool, bool]]:
    def within(age: timedelta, window: timedelta, inclusive: bool) -> bool:
        if age < timedelta():
            return False
        return age <= window if inclusive else age < window

    decisions: dict[str, tuple[bool, bool]] = {}
    for _, doc in EVIDENCE_CASES:
        context = doc.get("dedupContext")
        if context is None:
            continue
        now = datetime.fromisoformat(context["now"])
        fingerprint = doc["candidate"]["fingerprint"]
        sealed_hit = any(
            entry["fingerprint"] == fingerprint
            and within(now - datetime.fromisoformat(entry["sealedAt"]), sealed, sealed_inclusive)
            for entry in context["priorSealedCandidates"])
        published_hit = any(
            entry["fingerprint"] == fingerprint
            and within(
                now - datetime.fromisoformat(entry["publishedAt"]), published,
                published_inclusive)
            for entry in context["publishedFingerprints"])
        decisions[doc["id"]] = (sealed_hit, published_hit)
    return decisions


def _expected_window_decisions() -> dict[str, tuple[bool, bool]]:
    expected: dict[str, tuple[bool, bool]] = {}
    for _, doc in EVIDENCE_CASES:
        if "dedupContext" not in doc:
            continue
        codes = set(doc["expected"]["reasonCodes"])
        expected[doc["id"]] = (
            "duplicate_sealed_candidate_within_seven_days" in codes,
            "duplicate_published_fingerprint_within_fourteen_days" in codes)
    return expected


def test_declared_dedup_windows_reproduce_the_corpus() -> None:
    declared = _window_decisions(SEALED_WINDOW, PUBLISHED_WINDOW)
    assert declared == _expected_window_decisions()
    assert declared, "the corpus needs at least one case carrying a dedupContext"


def test_only_the_declared_integer_day_windows_reproduce_the_corpus() -> None:
    expected = _expected_window_decisions()
    reproducing = [
        (sealed, published)
        for sealed in range(_MAX_SWEEP_DAYS + 1)
        for published in range(_MAX_SWEEP_DAYS + 1)
        if _window_decisions(timedelta(days=sealed), timedelta(days=published)) == expected]
    assert reproducing == [(SEALED_WINDOW.days, PUBLISHED_WINDOW.days)], (
        f"these day pairs all reproduce the corpus, so the windows are not pinned: {reproducing}")


@pytest.mark.parametrize(
    ("sealed_days", "published_days"),
    [(7, 7), (14, 14), (14, 7), (6, 14), (8, 14), (7, 13), (7, 15)])
def test_wrong_dedup_windows_fail_the_corpus(sealed_days: int, published_days: int) -> None:
    assert (sealed_days, published_days) != (SEALED_WINDOW.days, PUBLISHED_WINDOW.days)
    decisions = _window_decisions(timedelta(days=sealed_days), timedelta(days=published_days))
    assert decisions != _expected_window_decisions(), (
        f"an engine using a {sealed_days} day sealed window and a {published_days} day published "
        f"window reproduces the corpus, so the corpus does not pin the real windows")


@pytest.mark.parametrize("epsilon", [timedelta(minutes=1), timedelta(hours=1)])
def test_windows_are_pinned_to_the_minute(epsilon: timedelta) -> None:
    """The cases one minute past each edge pin the window to the minute, not just to the day."""
    expected = _expected_window_decisions()
    for label, sealed, published in (
        ("sealed short", SEALED_WINDOW - epsilon, PUBLISHED_WINDOW),
        ("sealed long", SEALED_WINDOW + epsilon, PUBLISHED_WINDOW),
        ("published short", SEALED_WINDOW, PUBLISHED_WINDOW - epsilon),
        ("published long", SEALED_WINDOW, PUBLISHED_WINDOW + epsilon),
    ):
        assert _window_decisions(sealed, published) != expected, (
            f"a {label} window off by {epsilon} still reproduces the corpus")


@pytest.mark.parametrize(
    ("sealed_inclusive", "published_inclusive"), [(False, True), (True, False), (False, False)])
def test_exclusive_window_comparison_fails_the_corpus(
    sealed_inclusive: bool, published_inclusive: bool,
) -> None:
    """An engine comparing with `<` instead of `<=` misses the case sitting exactly on the edge."""
    decisions = _window_decisions(
        SEALED_WINDOW, PUBLISHED_WINDOW, sealed_inclusive=sealed_inclusive,
        published_inclusive=published_inclusive)
    assert decisions != _expected_window_decisions(), (
        "the dedup windows are inclusive, so a strict less than comparison must fail the corpus")


def test_each_dedup_dimension_pins_all_four_positions() -> None:
    """Inside, outside, exactly on the edge, and one minute past it, for each window."""
    positions: dict[str, set[str]] = {"sealed": set(), "published": set()}
    for _, doc in EVIDENCE_CASES:
        context = doc.get("dedupContext")
        if context is None:
            continue
        now = datetime.fromisoformat(context["now"])
        fingerprint = doc["candidate"]["fingerprint"]
        for dimension, entries, stamp, window in (
            ("sealed", context["priorSealedCandidates"], "sealedAt", SEALED_WINDOW),
            ("published", context["publishedFingerprints"], "publishedAt", PUBLISHED_WINDOW),
        ):
            for entry in entries:
                if entry["fingerprint"] != fingerprint:
                    continue
                age = now - datetime.fromisoformat(entry[stamp])
                if age == window:
                    positions[dimension].add("exactly at the edge")
                elif window < age <= window + timedelta(minutes=1):
                    positions[dimension].add("just past the edge")
                elif age < window:
                    positions[dimension].add("inside")
                else:
                    positions[dimension].add("outside")
    required = {"inside", "outside", "exactly at the edge", "just past the edge"}
    for dimension, found in positions.items():
        assert found == required, (
            f"the {dimension} window is missing cases {sorted(required - found)}")


def test_published_duplicate_case_sits_between_the_two_windows() -> None:
    """The published duplicate has to be older than the sealed window, or a seven day engine
    would catch it for the wrong reason and the fourteen day window would never be exercised."""
    checked = 0
    for _, doc in EVIDENCE_CASES:
        codes = set(doc["expected"]["reasonCodes"])
        context = doc.get("dedupContext")
        if context is None:
            continue
        now = datetime.fromisoformat(context["now"])
        fingerprint = doc["candidate"]["fingerprint"]
        ages = [
            now - datetime.fromisoformat(entry["publishedAt"])
            for entry in context["publishedFingerprints"]
            if entry["fingerprint"] == fingerprint]
        if "duplicate_published_fingerprint_within_fourteen_days" in codes:
            assert any(SEALED_WINDOW < age <= PUBLISHED_WINDOW for age in ages), (
                f"{doc['id']} must place its published duplicate strictly between the seven day "
                f"and fourteen day windows")
            checked += 1
        if "dedup_outside_published_window" in codes:
            assert ages and all(age > PUBLISHED_WINDOW for age in ages), (
                f"{doc['id']} must place its published match outside the fourteen day window")
            checked += 1
    assert checked >= 2, "both sides of the published fingerprint window need a case"


def test_sealed_duplicate_case_sits_inside_the_shorter_window() -> None:
    checked = 0
    for _, doc in EVIDENCE_CASES:
        codes = set(doc["expected"]["reasonCodes"])
        context = doc.get("dedupContext")
        if context is None:
            continue
        now = datetime.fromisoformat(context["now"])
        fingerprint = doc["candidate"]["fingerprint"]
        ages = [
            now - datetime.fromisoformat(entry["sealedAt"])
            for entry in context["priorSealedCandidates"]
            if entry["fingerprint"] == fingerprint]
        if "duplicate_sealed_candidate_within_seven_days" in codes:
            assert any(timedelta() <= age <= SEALED_WINDOW for age in ages), doc["id"]
            checked += 1
        if "dedup_outside_sealed_window" in codes:
            assert ages and all(SEALED_WINDOW < age <= PUBLISHED_WINDOW for age in ages), (
                f"{doc['id']} must place its sealed match outside the seven day window while "
                f"staying inside the fourteen day one, so the two windows stay distinguishable")
            checked += 1
    assert checked >= 2, "both sides of the sealed candidate window need a case"


# --- Run cases ----------------------------------------------------------------------------------


@pytest.mark.parametrize("doc", RUN_PARAMS)
def test_run_case_shape_and_cap(doc: dict) -> None:
    document = SCHEMA["documents"]["run_case"]
    _require_field_order(doc, document["fieldOrder"], document["required"], doc["id"])
    run, queue, expected = doc["run"], doc["queue"], doc["expected"]
    _require_field_order(run, document["runFieldOrder"], document["runRequired"], f"{doc['id']}.run")
    _require_field_order(
        expected, document["expectedFieldOrder"], document["expectedRequired"],
        f"{doc['id']}.expected")

    assert run["mode"] in VOCABULARIES["runMode"]
    assert run["publishedCap"] == SCHEMA["batchCaps"][run["mode"]]
    datetime.strptime(run["slotDate"], "%Y-%m-%d")

    assert [item["rank"] for item in queue] == list(range(1, len(queue) + 1)), (
        f"{doc['id']} queue must be ranked from one with no gaps")
    for item in queue:
        _require_field_order(
            item, document["queueFieldOrder"], document["queueRequired"],
            f"{doc['id']}.queue[{item['candidateId']}]")
        assert item["recordKind"] in VOCABULARIES["supportedRecordKind"]
        assert item["eligibility"] == "publishable"

    ordered_ids = [item["candidateId"] for item in queue]
    assert len(set(ordered_ids)) == len(ordered_ids)
    cap = run["publishedCap"]
    assert expected["publishedCandidateIds"] == ordered_ids[:cap], (
        f"{doc['id']} must publish the highest ranked candidates up to the cap")
    assert expected["deferredCandidateIds"] == ordered_ids[cap:], (
        f"{doc['id']} must defer the overflow in rank order")
    assert set(expected["deferralReasonByCandidateId"]) == set(expected["deferredCandidateIds"])
    assert set(expected["deferralReasonByCandidateId"].values()) <= {"batch_cap_reached"}

    _sorted_unique(expected["reasonCodes"], f"{doc['id']} reasonCodes")
    unknown = [code for code in expected["reasonCodes"] if code not in REASON_CODES]
    assert not unknown, f"{doc['id']} uses reason codes outside the closed vocabulary: {unknown}"
    overflow = bool(expected["deferredCandidateIds"])
    assert ("batch_cap_reached" in expected["reasonCodes"]) == overflow
    assert ("batch_cap_within_limit" in expected["reasonCodes"]) != overflow
    assert (doc["polarity"] == "negative") == overflow, (
        f"{doc['id']} polarity does not match whether the cap deferred anything")
