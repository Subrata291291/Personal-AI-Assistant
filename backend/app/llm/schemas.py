
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

    @property
    def output_limited(self) -> bool:
        """Identify provider finish reasons that indicate a cut-off output."""
        reason = (self.finish_reason or "").strip().casefold()
        return (
            reason in {"length", "max_tokens", "max token"}
            or "max_tokens" in reason
            or "max tokens" in reason
            or reason.endswith(".length")
        )

    def completion_metadata(self) -> dict[str, Any]:
        reason = (self.finish_reason or "").strip().casefold()
        if self.output_limited:
            status = "truncated"
        elif (
            reason in {"stop", "end_turn", "completed", "stop_sequence"}
            or reason.endswith((".stop", ".end_turn"))
        ):
            status = "complete"
        else:
            status = "unknown"

        return {
            "status": status,
            "finish_reason": self.finish_reason,
            "usage": (
                self.usage.model_dump(exclude_none=True)
                if self.usage is not None
                else None
            ),
        }


def provider_output_token_limit(
    requested_limit: int | None,
    provider_limit: int,
) -> int:
    if requested_limit is None:
        return provider_limit
    return min(requested_limit, provider_limit)
