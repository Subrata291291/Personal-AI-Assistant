
import time

from groq import Groq

from app.core.config import settings
from app.llm.providers.base import BaseLLMProvider
from app.llm.schemas import (
    LLMRequest,
    LLMResponse,
    LLMUsage,
    provider_output_token_limit,
)


class GroqProvider(BaseLLMProvider):

    provider_name = "groq"

    def __init__(self):
        if not settings.GROQ_API_KEY:
            raise RuntimeError("Groq API key is not configured.")

        if not settings.GROQ_MODEL:
            raise RuntimeError("Groq model is not configured.")

        self.client = Groq(
            api_key=settings.GROQ_API_KEY.get_secret_value(),
            timeout=settings.GROQ_TIMEOUT_SECONDS,
            max_retries=0,
        )

        self.model = settings.GROQ_MODEL

    def generate(self, request: LLMRequest) -> LLMResponse:
        start_time = time.perf_counter()

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": message.role,
                    "content": message.content,
                }
                for message in request.messages
            ],
            temperature=request.temperature,
            max_tokens=provider_output_token_limit(
                request.max_tokens,
                settings.GROQ_MAX_OUTPUT_TOKENS,
            ),
        )

        latency_ms = (time.perf_counter() - start_time) * 1000

        if not response.choices:
            raise RuntimeError("Groq returned an empty response.")

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
