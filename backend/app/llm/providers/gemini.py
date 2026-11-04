
import time

from google import genai
from google.genai import types

from app.core.config import settings
from app.llm.providers.base import BaseLLMProvider
from app.llm.schemas import (
    LLMRequest,
    LLMResponse,
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

    def generate(self, request: LLMRequest) -> LLMResponse:
        start_time = time.perf_counter()

        contents = [
            types.Content(
                role=(
                    "model"
                    if message.role == "assistant"
                    else "user"
                ),
                parts=[types.Part.from_text(text=message.content)],
            )
            for message in request.messages
            if message.role != "system"
        ]

        system_instruction = "\n".join(
            message.content
            for message in request.messages
            if message.role == "system"
        ) or None

        response = self.client.models.generate_content(
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
                    prompt_tokens=usage_metadata.prompt_token_count,
                    completion_tokens=usage_metadata.candidates_token_count,
                    total_tokens=usage_metadata.total_token_count,
                )
                if usage_metadata
                else None
            ),
            finish_reason=(
                str(response.candidates[0].finish_reason)
                if response.candidates
                else None
            ),
        )
