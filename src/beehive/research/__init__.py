# src/beehive/research/__init__.py
"""Research planning foundation (ADR-0006, ADR-0007, ADR-0008, ADR-0009, ADR-0010).

ADR-0007 is the load-bearing rule for this whole package: Research AI may propose and revise a
Research Plan, but it never invokes a connector or any other tool directly. Every module here
follows the same three-part discipline:

1. Every Research AI call goes through `beehive.ai.llm_client.run_data_only_prompt` -- never
   the tool-permissive `run_prompt` -- so a prompt-injection payload hidden in the Research
   Question, a prior plan, or any other untrusted text has no tool to reach (see
   `llm_client.py`'s own docstring for how that guarantee is verified rather than assumed).
2. The AI's only output is inert structured JSON. `structured_response.py` holds the shared
   strict-parsing primitives (fenced-JSON extraction, exact top-level shape, bounded
   strings/lists, typed errors) that every Research AI response parser in this package is
   built from, so "reject anything that doesn't match" is enforced identically everywhere
   rather than each parser reinventing its own edge cases.
3. `connector_policy.py` is the explicit allowlist of which connector types a Research Plan may
   use and the exact config schema each one accepts. The application -- never the AI --
   normalizes and validates every proposed Research Source against this allowlist, then calls
   the connector's own `validate_config` as the final authority, before anything is persisted
   or executed.

`planner.py` is the first concrete AI call built on top of (1)-(3): it proposes an initial
Research Plan from a Research Question, and later revises a plan given the Research Question
(immutable for the Research Session), the prior plan already visible to the Owner, and any
coverage gaps identified since then.

`limits.py` holds every hard numeric ceiling this package (and later Research modules) enforce:
Research Plan size, config/rationale/summary lengths, a Research Run's 20-minute duration
ceiling, and its 30-deep-fetch ceiling.

This package is intentionally decoupled from `beehive.db` and `beehive.domain`: it returns its
own plain, frozen dataclasses (e.g. `planner.ResearchPlan`), field-for-field compatible with
`beehive.domain.research`'s `ResearchSource`/`ResearchPlanRevision` shapes, so a later
persistence/orchestration layer can map them across that boundary without this package needing
to import or know anything about storage."""
from __future__ import annotations
