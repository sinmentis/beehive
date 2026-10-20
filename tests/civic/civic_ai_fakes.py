"""Offline doubles for the Civic AI stages: a scripted gateway and valid-response builders.

`FakeCivicGateway` is the injectable seam `beehive.civic.ai_pipeline` takes as `gateway`. It is a
test double and lives here, in the test tree, deliberately: production has exactly one gateway
(`CopilotCivicGateway`) and no fallback path, so there is no way for a misconfigured deployment
to end up generating civic statements from a stub.

The gateway records every `CivicModelRequest` it is handed, which is what lets a test assert the
invariants that matter more than any single response: that no request ever carries a tool, that
each stage got the model it was supposed to, that the call count really is two per Candidate, and
that no write lock is held while a call is in flight (`on_call`, invoked from inside `complete`).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from beehive.civic.ai_gateway import (CIVIC_MODEL_PROVIDER, CivicAiStage, CivicModelError,
                                      CivicModelRequest, CivicModelResponse)
from beehive.civic.limits import REQUIRED_LOCALES, CivicLocale


def fenced(payload: Any) -> str:
    """A model reply in the only shape the parsers accept: one fenced json object."""
    return "Here is the result.\n```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"


def content_payload(
    *,
    title: str,
    summary: str,
    event_date: str,
    claims: list[dict],
    entities: list[str],
    policy_status: str = "announced",
    uncertainty: bool = False,
) -> dict:
    return {
        "title": title,
        "summary": summary,
        "event_date": event_date,
        "policy_status": policy_status,
        "uncertainty": uncertainty,
        "entities": entities,
        "claims": claims,
    }


def verification_payload(verdict: str = "pass", issues: list[dict] | None = None) -> dict:
    return {"verdict": verdict, "issues": issues or []}


@dataclass
class RecordedCall:
    request: CivicModelRequest
    stage: CivicAiStage
    model: str


@dataclass
class FakeCivicGateway:
    """A gateway whose answers are decided by the test, not by a model.

    `responses` maps a stage to either a raw reply string, an exception to raise, or a callable
    taking the request and returning either. A stage with no entry raises, so a test can never
    accidentally pass because an unexpected stage got a default answer.

    `fail_once` is the transient-failure script the retry tests need: the first call to each
    listed stage raises the given `CivicModelError`, and every later call is served normally."""

    responses: dict[CivicAiStage, Any] = field(default_factory=dict)
    calls: list[RecordedCall] = field(default_factory=list)
    on_call: Callable[[CivicModelRequest], None] | None = None
    fail_once: dict[CivicAiStage, CivicModelError] = field(default_factory=dict)
    failed_stages: set = field(default_factory=set)

    async def complete(self, request: CivicModelRequest) -> CivicModelResponse:
        self.calls.append(
            RecordedCall(request=request, stage=request.stage, model=request.model))
        if self.on_call is not None:
            self.on_call(request)
        if request.stage in self.fail_once and request.stage not in self.failed_stages:
            self.failed_stages.add(request.stage)
            raise self.fail_once[request.stage]
        if request.stage not in self.responses:
            raise AssertionError(f"no scripted response for stage {request.stage.value}")
        reply = self.responses[request.stage]
        if isinstance(reply, BaseException):
            raise reply
        if callable(reply):
            reply = reply(request)
        if isinstance(reply, BaseException):
            raise reply
        if not isinstance(reply, str):
            raise CivicModelError("model_empty_response", "scripted reply was not text")
        return CivicModelResponse(
            text=reply, model=request.model, provider=CIVIC_MODEL_PROVIDER)

    @property
    def stages(self) -> list[CivicAiStage]:
        return [call.stage for call in self.calls]

    def requests_for(self, stage: CivicAiStage) -> list[CivicModelRequest]:
        return [call.request for call in self.calls if call.stage is stage]


# One consistent story told in four languages, citing the same alias in the same claim, with the
# same ISO date and the same numbers. This is what a passing four-locale Candidate looks like.
LOCALE_TEXT = {
    CivicLocale.ZH: {
        "title": "议会通过住房法案二读",
        "summary": "记录显示该法案于 2026-08-01 通过二读。",
        "claim": "该法案于 2026-08-01 通过二读。",
    },
    CivicLocale.EN: {
        "title": "Housing bill passes second reading",
        "summary": "The record shows the bill passed its second reading on 2026-08-01.",
        "claim": "The bill passed its second reading on 2026-08-01.",
    },
    CivicLocale.HI: {
        "title": "आवास विधेयक दूसरी बार पारित",
        "summary": "रिकॉर्ड बताता है कि विधेयक 2026-08-01 को दूसरी बार पारित हुआ।",
        "claim": "विधेयक 2026-08-01 को दूसरी बार पारित हुआ।",
    },
    CivicLocale.TL: {
        "title": "Naipasa ang panukalang pabahay sa ikalawang pagbasa",
        "summary": "Ipinapakita ng talaan na naipasa ito noong 2026-08-01.",
        "claim": "Naipasa ang panukala noong 2026-08-01.",
    },
}


def locale_entry(
    locale: CivicLocale,
    *,
    event_date: str = "2026-08-01",
    citations: list[str] | None = None,
    entities: list[str] | None = None,
    policy_status: str = "passed",
    uncertainty: bool = False,
    title: str | None = None,
    summary: str | None = None,
    claim_text: str | None = None,
    extra_claims: list[dict] | None = None,
) -> dict:
    """One locale object inside a generation entry, with every field a test may need to spoil."""
    text = LOCALE_TEXT[locale]
    claims = [{
        "text": claim_text if claim_text is not None else text["claim"],
        "citations": list(citations or ["e1"]),
    }]
    claims.extend(extra_claims or [])
    return content_payload(
        title=title if title is not None else text["title"],
        summary=summary if summary is not None else text["summary"],
        event_date=event_date, claims=claims,
        entities=list(entities if entities is not None else ["green-party"]),
        policy_status=policy_status, uncertainty=uncertainty)


def full_entry(**locale_overrides: Any) -> dict:
    """All four locales, keyed by locale code, with per-locale overrides applied by name."""
    return {
        locale.value: locale_entry(locale, **locale_overrides.get(locale.value, {}))
        for locale in REQUIRED_LOCALES
    }


def generation_payload(
    decision: str = "qualify",
    exclusion: str | None = None,
    reason: str = "Durable civic fact",
    entry: dict | None = None,
) -> dict:
    """The whole `generate` response: a screening decision and, when it qualifies, four locales."""
    if decision == "qualify" and entry is None:
        entry = full_entry()
    return {
        "decision": decision,
        "editorial_exclusion": exclusion,
        "reason": reason,
        "entry": entry,
    }


def generation_reply(**overrides: Any) -> str:
    """A valid `generate` reply, optionally with one locale spoiled or dropped."""
    locale_overrides = overrides.pop("locale_overrides", {})
    dropped = overrides.pop("drop_locale", None)
    entry = full_entry(**{key: value for key, value in locale_overrides.items()})
    if dropped is not None:
        entry.pop(dropped.value, None)
    return fenced(generation_payload(entry=entry, **overrides))


def refusal_reply(decision: str = "discard", exclusion: str = "poll",
                  reason: str = "This is a poll") -> str:
    return fenced(generation_payload(decision=decision, exclusion=exclusion, reason=reason))


def passing_gateway(**overrides: Any) -> FakeCivicGateway:
    """A gateway that produces a complete, consistent, verifiable four-locale Candidate."""
    responses: dict[CivicAiStage, Any] = {
        CivicAiStage.GENERATE: generation_reply(),
        CivicAiStage.VERIFICATION: fenced(verification_payload()),
    }
    responses.update(overrides)
    return FakeCivicGateway(responses=responses)
