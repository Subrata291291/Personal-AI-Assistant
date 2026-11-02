
import time

from openai import OpenAI

from app.core.config import settings
from app.llm.providers.base import BaseLLMProvider
from app.llm.schemas import LLMRequest, LLMResponse, LLMUsage


class OpenRouterProvider(BaseLLMProvider):

    provider_name = "openrouter"

    def __init__(self):
        if not settings.OPENROUTER_API_KEY:
            raise RuntimeError("OpenRouter API key is not configured.")

        if not settings.OPENROUTER_MODEL:
            raise RuntimeError("OpenRouter model is not configured.")

        self.client = OpenAI(
            api_key=settings.OPENROUTER_API_KEY.get_secret_value(),
            base_url="https://openrouter.ai/api/v1",
            timeout=settings.OPENROUTER_TIMEOUT_SECONDS,
            max_retries=0,
        )

        self.model = settings.OPENROUTER_MODEL

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
            max_tokens=request.max_tokens,
        )

        latency_ms = (time.perf_counter() - start_time) * 1000

        if not response.choices:
            raise RuntimeError("OpenRouter returned an empty response.")

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
