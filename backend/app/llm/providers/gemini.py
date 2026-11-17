
import time
from collections.abc import Iterator

from google import genai
from google.genai import types

from app.core.config import settings
from app.llm.providers.base import BaseLLMProvider
from app.llm.schemas import (
    LLMRequest,
    LLMResponse,
    LLMStreamChunk,
    LLMUsage,
    provider_output_token_limit,
)


class GeminiProvider(BaseLLMProvider):

    provider_name = "gemini"

    def __init__(self):
        if not settings.GEMINI_API_KEY:
            raise RuntimeError("Gemini API key is not configured.")

        if not settings.GEMINI_MODEL:
            raise RuntimeError("Gemini model is not configured.")

        self.client = genai.Client(
            api_key=settings.GEMINI_API_KEY.get_secret_value(),
            http_options=types.HttpOptions(
                timeout=settings.GEMINI_TIMEOUT_SECONDS * 1000,
                retry_options=types.HttpRetryOptions(
                    attempts=1,
                ),
            ),
        )

        self.model = settings.GEMINI_MODEL

    def _client_for_request(self, request: LLMRequest):
        timeout_override = getattr(request, "timeout_seconds", None)
        if not timeout_override:
            return self.client
        try:
            effective = min(
                float(settings.GEMINI_TIMEOUT_SECONDS),
                float(timeout_override),
            )
            if effective <= 0:
                return self.client
            return genai.Client(
                api_key=settings.GEMINI_API_KEY.get_secret_value(),
                http_options=types.HttpOptions(
                    timeout=int(max(1.0, effective) * 1000),
                    retry_options=types.HttpRetryOptions(attempts=1),
                ),
            )
        except Exception:
            return self.client

    def _contents_and_system(self, request: LLMRequest):
        contents = [
            types.Content(
                role=("model" if message.role == "assistant" else "user"),
                parts=[types.Part.from_text(text=message.content)],
            )
            for message in request.messages
            if message.role != "system"
        ]
        system_instruction = "\n".join(
            message.content for message in request.messages if message.role == "system"
        ) or None
        return contents, system_instruction

    def _config(self, request: LLMRequest) -> types.GenerateContentConfig:
        contents, system_instruction = self._contents_and_system(request)
        return types.GenerateContentConfig(
            temperature=request.temperature,
            max_output_tokens=provider_output_token_limit(
                request.max_tokens,
                settings.GEMINI_MAX_OUTPUT_TOKENS,
            ),
            system_instruction=system_instruction,
        )

    def generate(self, request: LLMRequest) -> LLMResponse:
        start_time = time.perf_counter()
        contents, system_instruction = self._contents_and_system(request)
        client = self._client_for_request(request)

        response = client.models.generate_content(
            model=self.model,
            contents=contents,
            config=types.GenerateContentConfig(
                temperature=request.temperature,
                max_output_tokens=provider_output_token_limit(
                    request.max_tokens,
                    settings.GEMINI_MAX_OUTPUT_TOKENS,
                ),
                system_instruction=system_instruction,
            ),
        )

        latency_ms = (time.perf_counter() - start_time) * 1000
        usage_metadata = response.usage_metadata

        return LLMResponse(
            content=response.text or "",
            provider=self.provider_name,
            model=self.model,
            latency_ms=latency_ms,
            usage=(
                LLMUsage(
                    prompt_tokens=getattr(usage_metadata, "prompt_token_count", None),
                    completion_tokens=getattr(usage_metadata, "candidates_token_count", None),
                    total_tokens=getattr(usage_metadata, "total_token_count", None),
                )
                if usage_metadata
                else None
            ),
            finish_reason=(
                str(response.candidates[0].finish_reason)
                if getattr(response, "candidates", None)
                else None
            ),
        )

    def stream(self, request: LLMRequest) -> Iterator[LLMStreamChunk]:
        contents, system_instruction = self._contents_and_system(request)
        client = self._client_for_request(request)
        stream = client.models.generate_content_stream(
            model=self.model,
            contents=contents,
            config=types.GenerateContentConfig(
                temperature=request.temperature,
                max_output_tokens=provider_output_token_limit(
                    request.max_tokens,
                    settings.GEMINI_MAX_OUTPUT_TOKENS,
                ),
                system_instruction=system_instruction,
            ),
        )
        last_finish: str | None = None
        last_usage: LLMUsage | None = None
        try:
            for chunk in stream:
                usage_metadata = getattr(chunk, "usage_metadata", None)
                if usage_metadata is not None:
                    last_usage = LLMUsage(
                        prompt_tokens=getattr(usage_metadata, "prompt_token_count", None),
                        completion_tokens=getattr(usage_metadata, "candidates_token_count", None),
                        total_tokens=getattr(usage_metadata, "total_token_count", None),
                    )
                candidates = getattr(chunk, "candidates", None) or []
                if candidates:
                    finish = getattr(candidates[0], "finish_reason", None)
                    if finish is not None:
                        last_finish = str(finish)
                text = getattr(chunk, "text", None) or ""
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
