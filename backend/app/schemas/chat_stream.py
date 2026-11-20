from typing import Any, Literal

from pydantic import BaseModel, Field


# These are the event names actually emitted by the chat /stream endpoint.
StreamEventType = Literal[
    "status",
    "token",
    "replace",
    "done",
    "error",
]


class ChatStreamEvent(BaseModel):
    """Structured SSE payload matching the chat streaming wire format."""

    type: StreamEventType
    conversation_id: str | None = None
    message_id: str | None = None
    user_message_id: str | None = None
    request_id: str | None = None
    provider: str | None = None
    model: str | None = None
    content: str | None = None
    status: str | None = None
    message: str | None = None
    partial: bool | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    response: dict[str, Any] | None = None
    completion: dict[str, Any] | None = None
    error: str | None = None

    def sse(self) -> str:
        return f"data: {self.model_dump_json(exclude_none=True)}\n\n"
