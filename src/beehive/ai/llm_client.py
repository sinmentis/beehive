# src/beehive/ai/llm_client.py
"""The ONLY file in this repo that imports `copilot` (github-copilot-sdk). A thin async wrapper
around the SDK: the import is lazy (inside each entry point) and CopilotClient() auto-authenticates
via COPILOT_GITHUB_TOKEN. The 120s default timeout suits a tool-free, single-shot call, not a
multi-round web-search research prompt.

Two entry points, two trust levels:

- `run_prompt` is the original ranking call. It grants the full built-in tool set and
  auto-approves every permission request (`PermissionHandler.approve_all`) because the ranking
  prompt is Beehive's own trusted text (profile + past votes + item digests it built itself).
  Left untouched so existing ranking behavior keeps working exactly as before.
- `run_data_only_prompt` is for prompts that embed untrusted, attacker-influenceable text
  (e.g. deep-read's extracted article body). It must be verifiably tool-free: no bash, no
  filesystem, no MCP, no ask_user -- a prompt-injection payload hidden in the article text
  must not be able to reach a single tool. It proves this by passing `available_tools=[]`,
  which the SDK docs (`CopilotClient.create_session`) state takes precedence over every other
  tool source (built-in, MCP, custom) and, empty, leaves nothing available. Belt-and-braces on
  top of that allowlist: `on_permission_request` denies (never approves) and
  `on_user_input_request` raises rather than answering, so that if some future SDK version ever
  routes a capability outside the `available_tools` gate, this fails loudly instead of quietly
  behaving like `approve_all`. Before any of that, `_require_tool_free_capability` inspects the
  installed SDK's `create_session` signature for the `available_tools` parameter; if a future or
  older SDK release doesn't expose it, this raises immediately -- callers must never fall back to
  an ungated, tool-permissive session for untrusted content."""
from __future__ import annotations

import inspect


def _decline_user_input(request: dict) -> dict:
    return {"response": "No human is available; proceed using your own best judgment."}


def _reject_user_input(request: dict, invocation: dict[str, str]) -> dict:
    raise RuntimeError(
        "Data-only Copilot session requested user input; no tool should be reachable in a "
        "tool-free session (available_tools=[]), so this indicates the tool-free guarantee "
        "did not hold. Refusing to answer rather than silently proceeding.")


def _deny_all_permissions(request: object, invocation: dict[str, str]):
    from copilot.rpc import PermissionDecisionReject

    return PermissionDecisionReject(
        feedback="Data-only Copilot session: tool execution is never permitted.")


def _require_tool_free_capability(client_cls: type) -> None:
    """Fail fast if the installed SDK can't express an empty tool allowlist.

    `available_tools` is what actually guarantees tool-free execution (see module docstring).
    If a future/older `github-copilot-sdk` release removes or renames this parameter, silently
    proceeding would mean `run_data_only_prompt` grants the full tool set to a session fed
    untrusted, attacker-influenceable text -- an explicit failure here is far safer than that.
    """
    params = inspect.signature(client_cls.create_session).parameters
    if "available_tools" not in params:
        raise RuntimeError(
            "Installed github-copilot-sdk's CopilotClient.create_session has no "
            "'available_tools' parameter, so tool-free execution cannot be guaranteed for "
            "this data-only prompt. Refusing to fall back to a tool-permissive session.")


async def _send_and_extract(session, prompt: str, timeout: float) -> str:
    from copilot.session_events import AssistantMessageData

    response = await session.send_and_wait(prompt, timeout=timeout)
    if response is None:
        raise RuntimeError("Copilot SDK returned no response before going idle")
    if not isinstance(response.data, AssistantMessageData):
        raise RuntimeError(
            f"Copilot SDK returned unexpected event data: {type(response.data).__name__}")
    return response.data.content


async def run_prompt(prompt: str, model: str = "claude-haiku-4.5", timeout: float = 120.0) -> str:
    """Trusted-prompt call used by ranking: full built-in tool set, auto-approved permissions."""
    from copilot import CopilotClient
    from copilot.session import PermissionHandler

    client = CopilotClient()
    try:
        await client.start()
        session = await client.create_session(
            model=model,
            on_permission_request=PermissionHandler.approve_all,
            on_user_input_request=_decline_user_input,
        )
        return await _send_and_extract(session, prompt, timeout)
    finally:
        await client.stop()


async def run_data_only_prompt(
        prompt: str, model: str = "claude-haiku-4.5", timeout: float = 120.0) -> str:
    """Tool-free call for prompts that embed untrusted text (e.g. a fetched article body).

    See the module docstring for why this needs its own entry point and how the tool-free
    guarantee is verified rather than assumed.
    """
    from copilot import CopilotClient

    _require_tool_free_capability(CopilotClient)

    client = CopilotClient()
    try:
        await client.start()
        session = await client.create_session(
            model=model,
            available_tools=[],
            on_permission_request=_deny_all_permissions,
            on_user_input_request=_reject_user_input,
        )
        return await _send_and_extract(session, prompt, timeout)
    finally:
        await client.stop()
