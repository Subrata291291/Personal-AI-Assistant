
import logging
from typing import Any

from app.core.config import settings
from app.llm.registry import ProviderRegistry
from app.llm.schemas import LLMRequest, LLMResponse


logger = logging.getLogger(__name__)


class LLMFallbackError(RuntimeError):
    """Raised when all eligible LLM providers fail."""


class FallbackManager:

    def __init__(self, registry: ProviderRegistry | None = None):
        self.registry = registry or ProviderRegistry()
        self.provider_priority = settings.llm_provider_priority_list

    @staticmethod
    
    @staticmethod
    def classify_error(error: Exception) -> tuple[bool, str, int | None]:
        """
        Returns:
            (is_transient, failure_category, status_code)
        """

        import socket
        from httpx import (
            TimeoutException,
            ConnectError,
            NetworkError,
        )

        
        status_code = getattr(error, "status_code", None)

        if not isinstance(status_code, int):
            status_code = getattr(error, "status", None)

        # Google Gemini SDK uses the "code" attribute.
        if not isinstance(status_code, int):
            status_code = getattr(error, "code", None)

        if not isinstance(status_code, int):
            status_code = None


        # HTTP status-based classification
        if status_code == 429:
            return True, "rate_limit", status_code

        if status_code == 408 or (
            status_code is not None and 500 <= status_code <= 599
        ):
            return True, "temporary_server_error", status_code

        if status_code is not None:
            return False, "non_retryable_http_error", status_code

        # Network and timeout exception classification
        if isinstance(
            error,
            (
                TimeoutException,
                ConnectError,
                NetworkError,
                TimeoutError,
                ConnectionError,
                socket.timeout,
            ),
        ):
            return True, "network_or_timeout", None

        # Some SDK exceptions expose an underlying network error.
        cause = error.__cause__ or error.__context__

        if cause is not None and cause is not error:
            is_transient, category, cause_status = (
                FallbackManager.classify_error(cause)
            )

            if is_transient:
                return True, category, cause_status

        return False, "non_retryable_error", None


    def generate(self, request: LLMRequest) -> LLMResponse:

        attempted_providers: list[str] = []
        last_error: Exception | None = None

        for provider_name in self.provider_priority:

            if provider_name not in {
                "groq", "openrouter", "gemini", "openai"
            }:
                raise RuntimeError(
                    f"Unsupported provider in priority configuration: {provider_name}"
                )

            
            if not self.registry.is_configured(provider_name):
                logger.warning(
                    "Skipping unconfigured LLM provider: %s",
                    provider_name,
                )
                continue

            attempted_providers.append(provider_name)

            try:
                provider = self.registry.get_provider(provider_name)


                logger.info(
                    "Attempting LLM provider: %s",
                    provider_name,
                )

                response = provider.generate(request)

                logger.info(
                    "LLM provider succeeded: %s | model=%s | latency_ms=%.2f",
                    response.provider,
                    response.model,
                    response.latency_ms,
                )

                return response

            except Exception as error:
                last_error = error

                is_transient, category, status_code = (
                    self.classify_error(error)
                )

                logger.warning(
                    "LLM provider failed: %s | category=%s | status=%s",
                    provider_name,
                    category,
                    status_code,
                )

                if not is_transient:
                    raise

                logger.info(
                    "Switching to the next configured LLM provider."
                )

        if not attempted_providers:
            raise LLMFallbackError(
                "No configured LLM providers are available."
            )

        raise LLMFallbackError(
            "All configured LLM providers failed. "
            f"Attempted providers: {', '.join(attempted_providers)}"
        ) from last_error
