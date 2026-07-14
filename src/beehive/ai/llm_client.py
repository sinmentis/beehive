# src/beehive/ai/llm_client.py
"""The ONLY file in this repo that imports `copilot` (github-copilot-sdk). A thin async wrapper
around the SDK: the import is lazy (inside run_prompt) and CopilotClient() auto-authenticates via
COPILOT_GITHUB_TOKEN. The 120s default timeout suits this tool-free, single-shot ranking call,
not a multi-round web-search research prompt."""
from __future__ import annotations


def _decline_user_input(request: dict) -> dict:
    return {"response": "No human is available; proceed using your own best judgment."}


async def run_prompt(prompt: str, model: str = "claude-haiku-4.5", timeout: float = 120.0) -> str:
    from copilot import CopilotClient
    from copilot.session import PermissionHandler
    from copilot.session_events import AssistantMessageData

    client = CopilotClient()
    try:
        await client.start()
        session = await client.create_session(
            model=model,
            on_permission_request=PermissionHandler.approve_all,
            on_user_input_request=_decline_user_input,
        )
        response = await session.send_and_wait(prompt, timeout=timeout)
        if response is None:
            raise RuntimeError("Copilot SDK returned no response before going idle")
        if not isinstance(response.data, AssistantMessageData):
            raise RuntimeError(
                f"Copilot SDK returned unexpected event data: {type(response.data).__name__}")
        return response.data.content
    finally:
        await client.stop()
