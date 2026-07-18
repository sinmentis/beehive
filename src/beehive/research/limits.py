# src/beehive/research/limits.py
"""Central, hard safety-ceiling numbers for the Research feature (ADR-0007). Every bound that
planner.py, connector_policy.py, or a later Research module (sufficiency assessment, synthesis,
run orchestration) needs to enforce lives here once, so a single number is never duplicated --
and never silently drifts -- across modules.

These are ceilings enforced in code, not targets the model is trusted to respect: a prompt may
ask for something smaller (e.g. "a short sentence"), but every value below is re-checked against
the actual response/request after the fact, exactly like ai/response_parser.py's existing
soft-cap fields."""
from __future__ import annotations

from datetime import timedelta

# --- Research Plan / Research Source bounds ---
# A Research Plan may add at most this many Research Sources in one plan/revision.
MAX_SOURCES_PER_PLAN = 8

# Any single connector config string value (subreddit, query, feed, sort, ...).
MAX_CONFIG_STRING_LENGTH = 200

# Per-source rationale text shown to the Owner alongside a proposed Research Source.
MAX_RATIONALE_LENGTH = 300

# The Research Plan's overall summary/rationale shown to the Owner.
MAX_PLAN_SUMMARY_LENGTH = 600

# --- Prior plan / sufficiency gaps fed into a revision prompt ---
# A revision prompt never echoes back more of the prior plan than a plan is itself allowed to
# contain -- reuse the same ceiling rather than inventing a second number.
MAX_PRIOR_SOURCES_IN_PROMPT = MAX_SOURCES_PER_PLAN
MAX_GAPS_IN_PROMPT = 10
MAX_GAP_LENGTH = 300

# --- Research Run ceilings (ADR-0009: one durable worker, database-enforced leases) ---
# Consumed by orchestrator.py, which reads a claimed run's own persisted deadline_at (set once
# by db/research_runs.py's claim_research_run) rather than re-deriving it from MAX_RUN_DURATION
# itself -- MAX_RUN_DURATION is the value the worker passes as claim_research_run's
# deadline_seconds, so it's defined here once and reused by both the claiming worker and this
# ceiling's only other reader, tests/research/test_limits.py.

# A processing Research Run's deadline_at is at most this far past started_at; the worker force
# -fails a run that overruns it rather than letting it run unbounded.
MAX_RUN_DURATION = timedelta(minutes=20)

# ResearchRun.deep_fetch_count ceiling for a single run -- the maximum number of full-article
# deep fetches one Research Run may perform while collecting evidence.
MAX_DEEP_FETCHES_PER_RUN = 30

# --- shared structured-response bounds (every Research AI parser reuses these rather than
# inventing its own numbers) ---
# A parse-time error list is itself capped so that a pathological response cannot make error
# reporting unbounded.
MAX_STRUCTURED_ERRORS = 20
MAX_ERROR_MESSAGE_LENGTH = 300

# --- Evidence collection / enrichment ceilings (research-orchestration) ---
# A single connector.fetch() call may return more RawItems than are worth carrying through
# enrichment/clustering/assessment in one run -- this bounds candidates considered per source,
# independent of and much smaller than MAX_DEEP_FETCHES_PER_RUN (which bounds full-text I/O,
# not how many snippet-only candidates are persisted).
MAX_CANDIDATES_PER_SOURCE = 25

# How many of a round's newly-collected candidates orchestrator.py asks enrichment.py to
# attempt a full-text deep fetch for, on top of (never instead of) the run-wide 30-fetch
# ceiling (MAX_DEEP_FETCHES_PER_RUN) that db/research_runs.py's reserve_deep_fetch enforces --
# this is a *per-round* throttle so one revision round can never claim the whole run's budget
# before Evidence Sufficiency ever gets a chance to stop early.
MAX_DEEP_FETCHES_PER_ROUND = 10

# --- Evidence text projected into an AI prompt (sufficiency.py) ---
# Distinct from deep_read/extract.py's own MAX_CHARS: this is the per-item ceiling applied
# on top of whatever full_text/snippet is already durably persisted (uncapped) for that
# Evidence Item, so a very long full-text article never dominates -- or blows the token
# budget of -- a prompt that must fit many evidence items at once.
MAX_EVIDENCE_TEXT_CHARS_IN_PROMPT = 1_500

# How many Evidence Items' projections one Evidence Sufficiency prompt may include. Combined
# with MAX_EVIDENCE_TEXT_CHARS_IN_PROMPT above, this is what keeps the prompt's total size
# bounded regardless of how many items a Research Session has accumulated across runs.
MAX_EVIDENCE_ITEMS_IN_SUFFICIENCY_PROMPT = 40

# --- Evidence Sufficiency response bounds (sufficiency.py) ---
MAX_SUB_QUESTIONS_IN_PROMPT = 10
MAX_SUB_QUESTION_LENGTH = 300
MAX_CONTRADICTIONS_IN_PROMPT = 10
MAX_CONTRADICTION_LENGTH = 300

# --- Research Run orchestration loop ceilings (orchestrator.py) ---
# A hard circuit breaker on plan-revision rounds, independent of (and always reached no later
# than) the run's deadline_at -- guards against a pathological/misconfigured deadline (or a
# test's frozen clock) letting the plan -> collect -> enrich -> cluster -> assess loop spin
# forever. ADR-0009's 20-minute MAX_RUN_DURATION is the primary ceiling; this is defense in
# depth underneath it.
MAX_REVISION_ROUNDS = 8

# Evidence Sufficiency stops the loop early once this many consecutive revision rounds add no
# material new evidence (a newly-collected item never seen before in this Research Session),
# even if the model has not yet reported the state as "sufficient" -- otherwise a Research
# Question with no more to find would spin until the deadline for no benefit.
NOVELTY_STOP_ROUNDS = 2

# --- Research Synthesis: core evidence-only, tool-free call (synthesis.py) ---
# Distinct ceilings from sufficiency.py's own MAX_EVIDENCE_ITEMS_IN_SUFFICIENCY_PROMPT /
# MAX_EVIDENCE_TEXT_CHARS_IN_PROMPT: a synthesis prompt is a separate AI call with its own
# per-call evidence alias table, so it re-caps independently rather than importing sufficiency's
# numbers, even though the values happen to start out equal.
MAX_EVIDENCE_ITEMS_IN_SYNTHESIS_PROMPT = 40
MAX_EVIDENCE_TEXT_CHARS_IN_SYNTHESIS_PROMPT = 1_500

# Each of the six core sections (bottom line, key findings, source agreements, source
# conflicts, unknowns, evidence coverage) may contain at most this many claims, and each
# claim's own text and citation-alias-list are capped by the two ceilings below.
MAX_CLAIMS_PER_SYNTHESIS_SECTION = 6
MAX_SYNTHESIS_CLAIM_TEXT_LENGTH = 400
MAX_CITATIONS_PER_SYNTHESIS_CLAIM = 6

# Evidence Sufficiency's own gaps/contradictions (already assessed by sufficiency.py) may be
# echoed into a synthesis prompt as read-only context -- reuses sufficiency.py's own ceilings
# rather than inventing a second number for the exact same kind of text.
MAX_PRIOR_GAPS_IN_SYNTHESIS_PROMPT = MAX_GAPS_IN_PROMPT
MAX_PRIOR_CONTRADICTIONS_IN_SYNTHESIS_PROMPT = MAX_CONTRADICTIONS_IN_PROMPT

# --- Research Synthesis: supplementary model-knowledge call (synthesis.py) ---
# This second, separate tool-free call never sees the collected evidence at all -- only these
# two ceilings bound its output, which is stored as its own isolated, citation-free claims,
# never mixed into or capable of changing the six core sections above.
MAX_MODEL_KNOWLEDGE_NOTES = 5
MAX_MODEL_KNOWLEDGE_NOTE_LENGTH = 400

# --- Research Conversation: reply generation (conversation.py) ---
# A distinct pair of ceilings from synthesis.py's own MAX_EVIDENCE_ITEMS_IN_SYNTHESIS_PROMPT /
# MAX_EVIDENCE_TEXT_CHARS_IN_SYNTHESIS_PROMPT: a chat reply is generated by its own tool-free
# call with its own per-call bounded evidence alias table, re-capped independently here even
# though the values happen to start out equal -- see synthesis.py's module docstring
# ("Aliases, never raw evidence_item_id") for why validating a citation against a larger,
# unbounded alias table than what was actually rendered would let a response cite an alias the
# model was never shown; conversation.py reuses that exact discipline for its own bounded set.
MAX_EVIDENCE_ITEMS_IN_CONVERSATION_PROMPT = 40
MAX_EVIDENCE_TEXT_CHARS_IN_CONVERSATION_PROMPT = 1_500

# A reply's evidence-backed claims: at most this many, each with its own bounded text and
# citation-alias-list.
MAX_CLAIMS_PER_CONVERSATION_REPLY = 6
MAX_CONVERSATION_CLAIM_TEXT_LENGTH = 500
MAX_CITATIONS_PER_CONVERSATION_CLAIM = 6

# A reply's separate, always-uncited supplementary model-knowledge notes -- same shape/
# discipline as synthesis.py's own model-knowledge call, kept as its own pair of constants for
# the same reason MAX_EVIDENCE_ITEMS_IN_CONVERSATION_PROMPT is its own constant above.
MAX_SUPPLEMENTARY_NOTES_PER_CONVERSATION_REPLY = 5
MAX_SUPPLEMENTARY_NOTE_LENGTH = 400

# How much prior conversation (earlier Owner questions and assistant replies) and how much of
# the pinned Research Synthesis's own claims a reply prompt echoes back as read-only context --
# independent of Conversation Memory, which is what keeps a VERY long Research Session's prompt
# bounded once the raw message history itself would no longer reasonably fit.
MAX_PRIOR_MESSAGES_IN_CONVERSATION_PROMPT = 20
MAX_MESSAGE_TEXT_CHARS_IN_CONVERSATION_PROMPT = 1_000
MAX_SYNTHESIS_CLAIMS_IN_CONVERSATION_PROMPT = 30
MAX_SYNTHESIS_CLAIM_TEXT_CHARS_IN_CONVERSATION_PROMPT = 400

# --- Conversation Memory: the hidden compaction call (conversation.py) ---
# CONTEXT.md's "Conversation Memory": an AI-maintained, hidden compression of earlier
# conversation. Capped so a very long Research Session's memory can never grow unbounded across
# many chat turns -- this is the one ceiling that keeps a long-running conversation's context
# bounded once the raw message history itself no longer fits in a prompt.
MAX_CONVERSATION_MEMORY_LENGTH = 2_000

# --- Diagnostics: capped raw-exception detail persisted on a failed Research Run/task ---
# Shared by orchestrator.py's own synthesis-failure capture and collector/research_worker.py's
# _classify_error: the ONLY diagnostic ever persisted for an unexpected failure is the
# exception's own type name plus its own message, capped to this length -- never the Research
# Question, evidence, or a raw prompt. One shared number here so neither call site can drift
# from the other.
MAX_ERROR_DETAIL_LENGTH = 1_000
