
from typing import Any, Literal

from pydantic import BaseModel, Field


MessageRole = Literal["system", "user", "assistant", "tool"]


class LLMMessage(BaseModel):
    role: MessageRole
    content: str


class LLMRequest(BaseModel):
    messages: list[LLMMessage] = Field(min_length=1)

    temperature: float = Field(
        default=0.7,
        ge=0.0,
        le=2.0,
    )

    max_tokens: int | None = Field(
        default=None,
        gt=0,
    )


class LLMUsage(BaseModel):
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


class LLMResponse(BaseModel):
    content: str

    provider: str
    model: str

    latency_ms: float = Field(ge=0)

    usage: LLMUsage | None = None

    finish_reason: str | None = None

    metadata: dict[str, Any] = Field(default_factory=dict)
