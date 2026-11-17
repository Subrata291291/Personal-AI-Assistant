from typing import Any

from langgraph.runtime import Runtime

from app.workflows.chat_state import ChatContext, ChatState


DRIVE_UNAVAILABLE_MESSAGE = (
    "Google Drive integration is temporarily unavailable and will be available "
    "in a future update."
)


def _response(message: str, state: ChatState) -> dict[str, Any]:
    return {
        "assistant_response": message,
        "provider": "",
        "model": "",
        "messages": [
            {"role": "user", "content": state["user_message"]},
            {"role": "assistant", "content": message},
        ],
    }


def drive_contracts_agent_node(
    state: ChatState,
    runtime: Runtime[ChatContext],
) -> dict[str, Any]:
    """Drive contracts are temporarily disabled for production readiness."""
    return _response(DRIVE_UNAVAILABLE_MESSAGE, state)
