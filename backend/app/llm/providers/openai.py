
import time
from collections.abc import Iterator

from openai import OpenAI

from app.core.config import settings
from app.llm.providers.base import BaseLLMProvider
from app.llm.schemas import (
    LLMRequest,
    LLMResponse,
    LLMStreamChunk,
    LLMUsage,
    provider_output_token_limit,
)


class OpenAIProvider(BaseLLMProvider):

    provider_name = "openai"

    def __init__(self):
        if not settings.OPENAI_API_KEY:
            raise RuntimeError("OpenAI API key is not configured.")

        if not settings.OPENAI_MODEL:
            raise RuntimeError("OpenAI model is not configured.")

        self.client = OpenAI(
            api_key=settings.OPENAI_API_KEY.get_secret_value(),
            timeout=settings.OPENAI_TIMEOUT_SECONDS,
            max_retries=0,
        )

        self.model = settings.OPENAI_MODEL

    def _messages(self, request: LLMRequest) -> list[dict[str, str]]:
        return [{"role": m.role, "content": m.content} for m in request.messages]

    def generate(self, request: LLMRequest) -> LLMResponse:
        start_time = time.perf_counter()
        client = self.client
        if getattr(request, "timeout_seconds", None):
            try:
                effective = float(request.timeout_seconds)
                client = self.client.with_options(
                    timeout=min(float(settings.OPENAI_TIMEOUT_SECONDS), effective)
                )
            except Exception:
                client = self.client

        response = client.chat.completions.create(
            model=self.model,
            messages=self._messages(request),
            temperature=request.temperature,
            max_tokens=provider_output_token_limit(
                request.max_tokens,
                settings.OPENAI_MAX_OUTPUT_TOKENS,
            ),
        )

        latency_ms = (time.perf_counter() - start_time) * 1000

        if not response.choices:
            raise RuntimeError("OpenAI returned an empty response.")

        choice = response.choices[0]
        usage = response.usage

        return LLMResponse(
            content=choice.message.content or "",
            provider=self.provider_name,
            model=self.model,
            latency_ms=latency_ms,
            usage=(
                LLMUsage(
                    prompt_tokens=usage.prompt_tokens,
                    completion_tokens=usage.completion_tokens,
                    total_tokens=usage.total_tokens,
                )
                if usage
                else None
            ),
            finish_reason=choice.finish_reason,
        )

    def stream(self, request: LLMRequest) -> Iterator[LLMStreamChunk]:
        client = self.client
        if getattr(request, "timeout_seconds", None):
            try:
                effective = float(request.timeout_seconds)
                client = self.client.with_options(
                    timeout=min(float(settings.OPENAI_TIMEOUT_SECONDS), effective)
                )
            except Exception:
                client = self.client
        stream = client.chat.completions.create(
            model=self.model,
            messages=self._messages(request),
            temperature=request.temperature,
            max_tokens=provider_output_token_limit(
                request.max_tokens,
                settings.OPENAI_MAX_OUTPUT_TOKENS,
            ),
            stream=True,
            stream_options={"include_usage": True},
        )
        last_finish: str | None = None
        last_usage: LLMUsage | None = None
        try:
            for event in stream:
                usage = getattr(event, "usage", None)
                if usage is not None:
                    last_usage = LLMUsage(
                        prompt_tokens=getattr(usage, "prompt_tokens", None),
                        completion_tokens=getattr(usage, "completion_tokens", None),
                        total_tokens=getattr(usage, "total_tokens", None),
                    )
                for choice in getattr(event, "choices", None) or []:
                    delta = getattr(choice, "delta", None)
                    text = getattr(delta, "content", None) if delta else None
                    finish = getattr(choice, "finish_reason", None)
                    if finish:
                        last_finish = finish
                    if text:
                        yield LLMStreamChunk(
                            content=text, provider=self.provider_name, model=self.model
                        )
            yield LLMStreamChunk(
                content="",
                provider=self.provider_name,
                model=self.model,
                finish_reason=last_finish,
                usage=last_usage,
                is_final=True,
            )
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
