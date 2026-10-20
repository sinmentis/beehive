# Civic evidence policy fixtures

Synthetic, copyright safe fixture corpus describing what the Civic evidence policy must decide,
one case per file. Nothing here imports or requires a production module: the corpus is plain
JSON, and `tests/civic/test_evidence_policy_fixtures.py` validates it with the standard library
only. The evidence engine itself is not implemented yet; these files are the contract it will be
built against.

## Safety rules for this directory

* Every party, publisher, agency, title, excerpt, and claim is invented.
* Every host sits under the reserved `.example` top level domain, so no fixture URL can ever
  resolve to a live third party site.
* No copied article text, no quotations, no real person, and no personal data of any kind.
* No em dash or en dash, matching the generated-text rule the pipeline applies downstream.

## Layout

```text
schema.json      versioned fixture schema: field order, closed vocabularies, reason codes, branches
publishers.json  synthetic publisher registry with ownership groups and ownership clusters
cases/           one evidence_case per file, named <id>.json
runs/            one run_case per file, named <id>.json
```

## Documents

`schema.json` carries `fixtureSchemaVersion: 1`. Every case file repeats that version, and the
test refuses a file whose version does not match. A breaking change to the shape bumps the
version in both places.

### `evidence_case`

One candidate, its input evidence, and the publication decision expected from it.

| Field | Meaning |
| --- | --- |
| `branches` | Policy branches this case exercises, from `schema.json.requiredBranches` |
| `polarity` | `positive` when the case is expected to publish, `negative` otherwise |
| `candidate` | Candidate facts: record kind, subject, record key, event date, category, durability |
| `evidence` | Input evidence items, sorted by `evidenceId` |
| `dedupContext` | Optional: `now`, prior sealed candidates, and published fingerprints |
| `retry` | Optional: the one bounded research attempt, as `before` / `additionalEvidence` / `after` |
| `expected` | Final decision: eligibility, outcome, reason codes, ownership groups, evidence split |

`expected.evidenceUsed` and `expected.evidenceExcluded` partition every evidence id in the case.
Discovery tier items can only ever appear in `evidenceExcluded`.

`expected.reasonCodes` is the **complete** set of codes the decision must carry, not a sample of
the codes the case was written to demonstrate. A publishable case therefore always records
`record_kind_supported` and `significance_durable` alongside the rule that established it, and a
media only case records the corroboration matches its cited publishers agree on. Two cases with
the same policy relevant inputs must record the same set, because the engine cannot tell them
apart; `tests/civic/test_evidence_policy_engine.py` compares corpus and engine for exact
equality, so a missing code and an unrelated extra code both fail.

When a case carries `retry`, `retry.maxAttempts` is `1`, `retry.before` is the decision before the
attempt, and `retry.after` is identical to the top level `expected`. That is what makes "one
bounded retry, then defer" checkable without an engine.

### `run_case`

One slot's ranked queue of already qualified candidates and the batch cap decision.

| Field | Meaning |
| --- | --- |
| `run` | Slot identity plus `mode` (`normal` or `election`) and `publishedCap` (6 or 10) |
| `queue` | Candidates in rank order, ranks `1..N` with no gaps |
| `expected` | Published ids, deferred ids, reason codes, and the deferral reason per candidate |

## Categories and tier rules

`schema.json.categoryEvidenceRules` records, per candidate category, which source tiers can
establish it and which plan rule says so. That table is what stops a fixture being vacuous: a
media ownership case categorised as `legislation` would defer on plan 4.2 rule 2 (a legal or
parliamentary state needs a T0 source) whatever its publishers were, so it would prove nothing
about ownership.

| Category | Established by | Plan rule |
| --- | --- | --- |
| `party-timeline-event` | T0 to T4, media only allowed | 3.1 durable party timeline event, 4.2 rule 4 |
| `party-policy` | T3 from the matching party | 4.2 rule 1 |
| `legislation` | T0 | 4.2 rule 2 |
| `official-finding` | T0 to T2 | 4.2 rule 3 |
| `electoral-administration` | T0 or T1 | 4.1 tiers |
| `correction` | T0 to T3 | 3.1 corrections |
| `electorate-boundary` | never publishable | 3.2 deferred scope |
| `poll`, `opinion`, `live-blog` | never publishable | 4.3 hard exclusions |

`party-timeline-event` is the only category whose evidence may be reputable media alone, so it
is the category every ownership, corroboration, paywall, accessibility, anonymous source, dedup,
discovery, and research retry case uses. `schema.json.mediaOnlyBranches` lists those branches and
the test suite refuses to let one of them sit on any other category.

The tier rules keep their own dedicated cases, listed in `schema.json.tierRuleCases`:
`legal-state-media-only-invalid` proves that two independent publishers still cannot establish a
legal state, `official-finding-official-review-valid` and `official-finding-party-only-invalid`
prove that a finding needs T0 to T2 rather than a party statement, and
`party-policy-without-party-primary-invalid` proves a policy needs its own party's source. Those
cases must keep their category, their evidence tiers, and their reason code.

`schema.json.dedupWindows` declares seven days for sealed candidates, fourteen days for published
fingerprints, and an inclusive edge: a match whose age equals the window exactly is a duplicate.
Each dimension carries four cases, so the corpus pins the window rather than merely exercising
it: comfortably inside, comfortably outside, exactly on the edge, and one minute past it. The
published duplicate deliberately sits about ten days back, outside the sealed window and inside
the published one, so a seven day window cannot stand in for it. The test suite sweeps every
integer day pair up to thirty and asserts that only seven and fourteen reproduce the corpus, that
a window off by one minute does not, and that comparing with `<` instead of `<=` misses the case
on the edge.

`schema.json.admissibilityRules` records the other ordering rule the corpus depends on. Discovery
tier material is removed before the evidence rules run, so `evidence_discovery_only_source` may
only appear when nothing admissible is left. A case that keeps one registered publisher alongside
a forum thread fails on `evidence_single_ownership_group`, because one ownership group cannot
carry a media only fact.

## Closed vocabularies

`schema.json.reasonCodes` is the closed reason code vocabulary. Each code carries a
`disposition`:

* `allow` codes may only appear on a `publishable` case;
* `block` codes appear on `blocked` cases, which are screened out or duplicates;
* `defer` codes appear on `deferred` cases, which keep their evidence for a later slot.

`schema.json.vocabularies` closes the remaining enums. Source tiers, source kinds, evidence
tiers, stances, and candidate outcomes reuse the values already defined by the Civic domain and
the destination dataset, so a fixture cannot describe a state the pipeline has no name for.

## Adding a case

1. Pick the branch or branches it exercises. A new branch must be added to
   `schema.json.requiredBranches` and needs both a positive and a negative case.
2. Reuse a publisher from `publishers.json`, or add one there first.
3. Name the file after its `id`, keep arrays sorted the way the schema describes, and keep the
   top level keys in `schema.json.documents.<kind>.fieldOrder` order.
4. Run `.venv/bin/python -m pytest tests/civic/test_evidence_policy_fixtures.py`.
