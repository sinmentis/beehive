"""The only way a Civic AI stage may reach a model, and the proof that it is tool-free.

Every Civic request goes through `CivicModelGateway.complete`, and the production implementation
is a thin adapter over `beehive.ai.llm_client.run_data_only_prompt` -- this package does not
build a second LLM stack, does not open its own SDK client, and does not know what a session is.
That matters because `run_data_only_prompt` is where the tool-free guarantee actually lives:
`available_tools=[]`, permission requests denied, user-input requests raised, and a startup check
that refuses to run at all on an SDK that cannot express an empty tool allowlist.

**A request cannot carry an endpoint or a credential.** `CivicModelRequest` has fields for the
stage, the prompt, a model id and a timeout, and nothing else: no base URL, no host, no API key,
no header map, no tool list. There is no field an injected string could populate to redirect a
call, and the model id is validated against `beehive.ai.model_catalog.BUILTIN_MODELS` before
the call, so a model name that arrived from data cannot become an arbitrary provider route.
Credentials remain entirely the SDK's business, read from the process environment it already
owns.

**The verifier has an explicit independent quality policy.** `verifier_model_for` pins a strong
supported model from a different family (plan 7.8). It rejects an unpinned automatic drafting
route instead of guessing its family, and never falls back to a weaker or same-family verifier.
The receipt records which concrete model ran.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from beehive.ai.model_catalog import BUILTIN_MODELS, LIST_TIMEOUT_SECONDS
from beehive.ai.model_selection import UnsupportedModelError
from beehive.civic.limits import AI_CALL_TIMEOUT_SECONDS

# The provider recorded on every stage receipt. One value today; a column rather than a constant
# because a receipt has to say which provider produced a statement, not which provider this build
# happened to be compiled against.
CIVIC_MODEL_PROVIDER = "github-copilot"

# A fixed list, not the refreshable account catalog: validation must not need a DB connection.
_ALLOWED_MODEL_IDS = frozenset(model.model_id for model in BUILTIN_MODELS)


class CivicAiStage(str, Enum):
    """The two AI stages, in execution order, and there are deliberately only two.

    `GENERATE` screens the material and, when it passes, writes the canonical Chinese entry and
    its three overlays in one strict response. `VERIFICATION` is the independent, differently
    modelled call that judges the result and cannot edit it.

    Splitting screening, drafting and each locale into separate calls -- the obvious shape -- put
    six calls behind every Candidate, which does not fit the plan's fixed ceiling of 30 AI calls
    per Run once the election cap of ten Candidates is in play. Two calls fit both caps with
    budget to spare for the single bounded transient retry: 12 for a normal slot, 20 for an
    election slot."""

    GENERATE = "generate"
    VERIFICATION = "verification"


class CivicModelError(RuntimeError):
    """A model call that did not produce a usable response: a timeout, a transport failure, a
    provider error. Distinct from a response that arrived and failed validation, because only
    this kind may consume the single bounded retry."""

    def __init__(self, error_code: str, detail: str) -> None:
        super().__init__(f"{error_code}: {detail}")
        self.error_code = error_code
        self.detail = detail


@dataclass(frozen=True)
class CivicModelRequest:
    """One tool-free model call.

    Deliberately minimal. There is no endpoint, no credential, no header, no tool definition and
    no function schema here, so nothing reachable from untrusted evidence text can redirect a
    call or grant a capability. `tools` exists only so the invariant can be asserted rather than
    argued about, and is required to be empty."""

    stage: CivicAiStage
    prompt: str
    model: str
    timeout: float = AI_CALL_TIMEOUT_SECONDS
    tools: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.prompt.strip():
            raise ValueError("a Civic model request needs a prompt")
        if self.tools:
            raise ValueError(
                "a Civic model request is tool-free; no tool, function or MCP server may be "
                "offered to a model that reads untrusted evidence text")
        if self.timeout <= 0:
            raise ValueError("a Civic model request needs a positive timeout")
        require_supported_model(self.model)


@dataclass(frozen=True)
class CivicModelResponse:
    """Exactly what the model said, plus which model said it. No parsing happens here."""

    text: str
    model: str
    provider: str = CIVIC_MODEL_PROVIDER


class CivicModelGateway(Protocol):
    """The seam every Civic AI stage calls, and the only thing a test has to substitute."""

    async def complete(self, request: CivicModelRequest) -> CivicModelResponse:
        ...


class CopilotCivicGateway:
    """Production gateway: one tool-free prompt per call, over the shared client.

    Holds no state beyond an optional reusable client, so it is safe to build per Run. Every call
    goes to `run_data_only_prompt`, which creates a fresh session, meaning no stage can see any
    other stage's conversation -- the independence the verifier stage depends on is structural
    rather than a matter of prompt wording."""

    def __init__(self, client: object | None = None) -> None:
        self._client = client

    async def complete(self, request: CivicModelRequest) -> CivicModelResponse:
        from beehive.ai.llm_client import run_data_only_prompt

        try:
            if self._client is not None:
                text = await run_data_only_prompt(
                    request.prompt, model=request.model, timeout=request.timeout,
                    client=self._client)
            else:
                text = await run_data_only_prompt(
                    request.prompt, model=request.model, timeout=request.timeout)
        except TimeoutError as exc:
            raise CivicModelError("model_timeout", str(exc)) from exc
        except CivicModelError:
            raise
        except RuntimeError as exc:
            raise CivicModelError("model_provider_error", str(exc)) from exc
        if not isinstance(text, str) or not text.strip():
            raise CivicModelError("model_empty_response", "the model returned no text")
        return CivicModelResponse(
            text=text, model=request.model, provider=CIVIC_MODEL_PROVIDER)


def require_supported_model(model: str) -> str:
    """The model id, validated against the application's own allowlist.

    A model id is the closest thing a Civic AI call has to a route, so it is never taken on
    trust: an id that is not on the allowlist raises instead of being passed to the SDK."""
    if model not in _ALLOWED_MODEL_IDS:
        raise UnsupportedModelError(f"Unsupported LLM model: {model!r}")
    return model


def verifier_model_for(drafting_model: str) -> str:
    """Pin a strong verifier from a different family, never lexical order or an automatic route."""
    require_supported_model(drafting_model)
    if drafting_model == "auto":
        raise UnsupportedModelError(
            "Civic verification requires a concrete drafting model to select an independent family")
    verifier = "gpt-6-astra" if _family(drafting_model) == "claude" else "claude-sonnet-5.5"
    return require_supported_model(verifier)


async def require_available_civic_models(drafting_model: str) -> None:
    from beehive.ai.llm_client import list_models

    required = (require_supported_model(drafting_model), verifier_model_for(drafting_model))
    async with asyncio.timeout(LIST_TIMEOUT_SECONDS):
        models = await list_models()
    available = {
        model.model_id for model in models if model.policy_state in {None, "enabled"}
    }
    missing = [model for model in required if model not in available]
    if missing:
        raise CivicModelError(
            "model_unavailable",
            f"Civic requires enabled account models: {', '.join(missing)}. "
            "No candidates were consumed; update the pinned model policy before retrying.")


def _family(model_id: str) -> str:
    return model_id.split("-", 1)[0]


__all__ = [
    "CIVIC_MODEL_PROVIDER",
    "CivicAiStage",
    "CivicModelError",
    "CivicModelGateway",
    "CivicModelRequest",
    "CivicModelResponse",
    "CopilotCivicGateway",
    "require_supported_model",
    "require_available_civic_models",
    "verifier_model_for",
]
