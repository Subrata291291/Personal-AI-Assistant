
from typing import Any, Literal

from pydantic import BaseModel, Field


StreamEventType = Literal[
    "message_start",
    "token",
    "status",
    "tool_start",
    "tool_complete",
    "approval_required",
    "message_complete",
    "error",
]


class ChatStreamEvent(BaseModel):
    """Structured SSE event for chat streaming."""

    event: StreamEventType
    conversation_id: str
    message_id: str | None = None
    request_id: str | None = None
    content: str | None = None
    status: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None

    def sse(self) -> str:
        return f"event: {self.event}\ndata: {self.model_dump_json(exclude_none=True)}\n\n"
