
import logging
import re
import socket
from collections.abc import Iterator
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from app.core.config import settings
from app.core.request_metrics import record_timing, request_id
from app.llm.registry import ProviderRegistry
from app.llm.schemas import LLMRequest, LLMResponse, LLMStreamChunk


logger = logging.getLogger(__name__)

_SUPPORTED_PROVIDERS = {"groq", "openrouter", "gemini", "openai"}
_SAFE_CODE_PATTERN = re.compile(r"[^a-zA-Z0-9_.:-]+")


@dataclass(frozen=True)
class ProviderFailureClassification:
    """Structured provider failure classification for fallback decisions."""

    fallback_to_next_provider: bool
    category: str
    status_code: int | None
    safe_error_code: str | None = None
    retry_same_provider: bool = False


class LLMFallbackError(RuntimeError):
    """Raised when all eligible LLM providers fail."""

    def __init__(
        self,
        message: str,
        *,
        attempted_providers: list[str] | None = None,
        failures: list[dict[str, Any]] | None = None,
    ):
        super().__init__(message)
        self.attempted_providers = attempted_providers or []
        self.failures = failures or []


class FallbackManager:

    def __init__(self, registry: ProviderRegistry | None = None):
        self.registry = registry or ProviderRegistry()
        self.provider_priority = settings.llm_provider_priority_list

    @staticmethod
    def _extract_status_code(error: Exception) -> int | None:
        for attr in ("status_code", "status", "code"):
            value = getattr(error, attr, None)
            if isinstance(value, int):
                return value
        response = getattr(error, "response", None)
        value = getattr(response, "status_code", None) if response is not None else None
        return value if isinstance(value, int) else None

    @staticmethod
    def _safe_error_code(error: Exception) -> str | None:
        # Extract only bounded, identifier-like metadata; never log raw messages/bodies.
        candidates: list[Any] = [getattr(error, "code", None)]
        body = getattr(error, "body", None)
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict):
                candidates.extend([err.get("code"), err.get("type")])
            elif isinstance(err, str):
                candidates.append(err)
            candidates.extend([body.get("code"), body.get("type")])
        response = getattr(error, "response", None)
        if response is not None and hasattr(response, "json"):
            try:
                payload = response.json()
                if isinstance(payload, dict):
                    err = payload.get("error")
                    if isinstance(err, dict):
                        candidates.extend([err.get("code"), err.get("type")])
                    candidates.extend([payload.get("code"), payload.get("type")])
            except Exception:
                # Never parse/log raw response text.
                pass
        for candidate in candidates:
            if candidate is None or isinstance(candidate, bool):
                continue
            if isinstance(candidate, int):
                return str(candidate)
            if isinstance(candidate, str):
                cleaned = _SAFE_CODE_PATTERN.sub("", candidate).strip("._:-")
                if cleaned:
                    return cleaned[:64]
        return None

    @staticmethod
    def _is_network_or_timeout(error: Exception) -> bool:
        from httpx import ConnectError, NetworkError, TimeoutException

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
            return True
        # OpenAI-compatible SDK connection/timeout exceptions (provider-agnostic).
        name = type(error).__name__
        module = type(error).__module__ or ""
        if "openai" in module and name in {"APIConnectionError", "APITimeoutError"}:
            return True
        if "groq" in module and name in {"APIConnectionError", "APITimeoutError"}:
            return True
        return False

    @classmethod
    def classify_provider_failure(cls, error: Exception) -> ProviderFailureClassification:
        """
        Classify provider failures for fallback decisions.

        Distinguishes:
        - retrying the same provider (not used automatically here; no added backoff), and
        - trying the next configured provider for provider-specific failures.

        Conservative policy: do not blindly fall back on likely shared request/input errors
        or application/programming errors.
        """

        status_code = cls._extract_status_code(error)
        safe_error_code = cls._safe_error_code(error)

        # Provider/server/network failures: safe to try another configured provider.
        if status_code == 429:
            return ProviderFailureClassification(
                fallback_to_next_provider=True,
                category="rate_limit",
                status_code=status_code,
                safe_error_code=safe_error_code,
            )

        if status_code == 408 or (status_code is not None and 500 <= status_code <= 599):
            return ProviderFailureClassification(
                fallback_to_next_provider=True,
                category="provider_server_error" if status_code and status_code >= 500 else "timeout",
                status_code=status_code,
                safe_error_code=safe_error_code,
            )

        # Explicit provider/account/model/access failures: non-retryable for this provider,
        # but eligible to try a different configured provider.
        if status_code == 401:
            return ProviderFailureClassification(
                fallback_to_next_provider=True,
                category="provider_auth_error",
                status_code=status_code,
                safe_error_code=safe_error_code,
            )
        if status_code == 402:
            return ProviderFailureClassification(
                fallback_to_next_provider=True,
                category="provider_payment_or_quota_error",
                status_code=status_code,
                safe_error_code=safe_error_code,
            )
        if status_code == 403:
            return ProviderFailureClassification(
                fallback_to_next_provider=True,
                category="provider_access_or_policy_error",
                status_code=status_code,
                safe_error_code=safe_error_code,
            )
        if status_code == 404:
            return ProviderFailureClassification(
                fallback_to_next_provider=True,
                category="provider_model_or_resource_unavailable",
                status_code=status_code,
                safe_error_code=safe_error_code,
            )

        # Likely request/input issues (possibly shared across providers) or unknown 4xx:
        # do not automatically fall back.
        if status_code is not None and 400 <= status_code <= 499:
            return ProviderFailureClassification(
                fallback_to_next_provider=False,
                category="request_error_non_fallback",
                status_code=status_code,
                safe_error_code=safe_error_code,
            )

        if cls._is_network_or_timeout(error):
            return ProviderFailureClassification(
                fallback_to_next_provider=True,
                category="network_or_timeout",
                status_code=status_code,
                safe_error_code=safe_error_code,
            )

        # Inspect nested cause/context for network/timeout signals from SDK wrappers.
        cause = error.__cause__ or error.__context__
        if cause is not None and cause is not error and isinstance(cause, Exception):
            try:
                nested = cls.classify_provider_failure(cause)
                if nested.fallback_to_next_provider and nested.category in {
                    "network_or_timeout",
                    "timeout",
                    "provider_server_error",
                    "rate_limit",
                }:
                    return ProviderFailureClassification(
                        fallback_to_next_provider=True,
                        category=nested.category,
                        status_code=nested.status_code or status_code,
                        safe_error_code=nested.safe_error_code or safe_error_code,
                    )
            except Exception:
                pass

        # Provider-side empty/invalid response surfaced by adapters: allow fallback.
        if isinstance(error, RuntimeError):
            msg = str(error).lower()
            if "returned an empty response" in msg:
                return ProviderFailureClassification(
                    fallback_to_next_provider=True,
                    category="provider_empty_response",
                    status_code=status_code,
                    safe_error_code=safe_error_code,
                )

        # Default: application/programming/unexpected errors must not be silently retried
        # across providers.
        return ProviderFailureClassification(
            fallback_to_next_provider=False,
            category="application_or_unexpected_error",
            status_code=status_code,
            safe_error_code=safe_error_code,
        )

    @staticmethod
    def classify_error(error: Exception) -> tuple[bool, str, int | None]:
        """Backward-compatible wrapper around classify_provider_failure."""
        classification = FallbackManager.classify_provider_failure(error)
        return (
            classification.fallback_to_next_provider,
            classification.category,
            classification.status_code,
        )

    @staticmethod
    def _provider_model(provider_name: str, provider: Any) -> str:
        model = getattr(provider, "model", None)
        if isinstance(model, str) and model.strip():
            return model
        config_model = {
            "groq": getattr(settings, "GROQ_MODEL", None),
            "openrouter": getattr(settings, "OPENROUTER_MODEL", None),
            "gemini": getattr(settings, "GEMINI_MODEL", None),
            "openai": getattr(settings, "OPENAI_MODEL", None),
        }.get(provider_name)
        return config_model if isinstance(config_model, str) else ""

    def _log_provider_attempt(
        self,
        *,
        provider_name: str,
        model: str,
        attempt_number: int,
        stream: bool,
    ) -> None:
        logger.info(
            "llm_provider_attempt_started | request_id=%s | provider=%s | model=%s "
            "| attempt=%s | stream=%s",
            request_id(),
            provider_name,
            model or "unknown",
            attempt_number,
            stream,
        )

    def _log_provider_failure(
        self,
        *,
        provider_name: str,
        model: str,
        attempt_number: int,
        error: Exception,
        classification: ProviderFailureClassification,
        fallback_will_continue: bool,
        stream: bool,
        content_emitted: bool = False,
        duration_ms: float | None = None,
    ) -> None:
        logger.warning(
            "llm_provider_attempt_failed | request_id=%s | provider=%s | model=%s "
            "| attempt=%s | stream=%s | duration_ms=%s | exception_class=%s.%s | status=%s "
            "| error_code=%s | category=%s | fallback_to_next_provider=%s "
            "| fallback_will_continue=%s | fallback_initiated=%s | content_emitted=%s",
            request_id(),
            provider_name,
            model or "unknown",
            attempt_number,
            stream,
            f"{duration_ms:.2f}" if isinstance(duration_ms, (int, float)) else "unknown",
            type(error).__module__,
            type(error).__name__,
            classification.status_code,
            classification.safe_error_code,
            classification.category,
            classification.fallback_to_next_provider,
            fallback_will_continue,
            fallback_will_continue,
            content_emitted,
        )

    @staticmethod
    def _failure_record(
        *,
        provider_name: str,
        model: str,
        attempt_number: int,
        error: Exception,
        classification: ProviderFailureClassification,
    ) -> dict[str, Any]:
        return {
            "provider": provider_name,
            "model": model or None,
            "attempt": attempt_number,
            "exception_class": f"{type(error).__module__}.{type(error).__name__}",
            "status_code": classification.status_code,
            "error_code": classification.safe_error_code,
            "category": classification.category,
            "fallback_to_next_provider": classification.fallback_to_next_provider,
        }

    @staticmethod
    def _overall_deadline_seconds(request: LLMRequest) -> float | None:
        configured = getattr(settings, "LLM_REQUEST_DEADLINE_SECONDS", None)
        if configured is None:
            return None
        try:
            value = float(configured)
        except Exception:
            return None
        return value if value > 0 else None

    @staticmethod
    def _request_with_timeout(request: LLMRequest, timeout_seconds: float) -> LLMRequest:
        try:
            return request.model_copy(update={"timeout_seconds": timeout_seconds})
        except Exception:
            return request

    def generate(self, request: LLMRequest) -> LLMResponse:

        attempted_providers: list[str] = []
        failures: list[dict[str, Any]] = []
        last_error: Exception | None = None
        overall_deadline_s = self._overall_deadline_seconds(request)
        overall_started_at = perf_counter()

        for provider_name in self.provider_priority:

            if provider_name not in _SUPPORTED_PROVIDERS:
                raise RuntimeError(
                    f"Unsupported provider in priority configuration: {provider_name}"
                )

            if not self.registry.is_configured(provider_name):
                logger.info(
                    "llm_provider_skipped_unconfigured | request_id=%s | provider=%s",
                    request_id(),
                    provider_name,
                )
                continue

            remaining_s = None
            if overall_deadline_s is not None:
                remaining_s = overall_deadline_s - (perf_counter() - overall_started_at)
                if remaining_s <= 0.5:
                    logger.warning(
                        "llm_provider_deadline_exhausted | request_id=%s | before_provider=%s "
                        "| remaining_s=%.3f | attempted_providers=%s",
                        request_id(),
                        provider_name,
                        remaining_s,
                        ",".join(attempted_providers),
                    )
                    raise LLMFallbackError(
                        "LLM request deadline exhausted before all providers could be attempted.",
                        attempted_providers=list(attempted_providers),
                        failures=failures,
                    ) from last_error

            attempt_request = (
                self._request_with_timeout(request, remaining_s)
                if remaining_s is not None
                else request
            )

            attempt_number = len(attempted_providers) + 1
            attempted_providers.append(provider_name)
            provider: Any | None = None
            provider_model = ""

            try:
                provider = self.registry.get_provider(provider_name)
                provider_model = self._provider_model(provider_name, provider)
                self._log_provider_attempt(
                    provider_name=provider_name,
                    model=provider_model,
                    attempt_number=attempt_number,
                    stream=False,
                )

                provider_started_at = perf_counter()
                try:
                    response = provider.generate(attempt_request)
                finally:
                    provider_duration_ms = (
                        perf_counter() - provider_started_at
                    ) * 1000
                    record_timing(
                        "llm_provider",
                        provider_duration_ms,
                    )
                    logger.info(
                        "llm_provider_attempt_completed | request_id=%s "
                        "| provider=%s | model=%s | attempt=%s | duration_ms=%.2f",
                        request_id(),
                        provider_name,
                        provider_model or "unknown",
                        attempt_number,
                        provider_duration_ms,
                    )

                logger.info(
                    "llm_provider_succeeded | request_id=%s | provider=%s | model=%s "
                    "| attempt=%s | latency_ms=%.2f",
                    request_id(),
                    response.provider,
                    response.model,
                    attempt_number,
                    response.latency_ms,
                )

                return response

            except Exception as error:
                last_error = error
                classification = self.classify_provider_failure(error)
                provider_duration_ms = (
                    (perf_counter() - provider_started_at) * 1000
                    if "provider_started_at" in locals()
                    else None
                )
                try:
                    setattr(error, "_llm_fallback_classification", classification)
                    setattr(error, "_llm_fallback_provider", provider_name)
                except Exception:
                    pass
                failure = self._failure_record(
                    provider_name=provider_name,
                    model=provider_model or (self._provider_model(provider_name, provider) if provider else ""),
                    attempt_number=attempt_number,
                    error=error,
                    classification=classification,
                )
                failures.append(failure)

                fallback_will_continue = classification.fallback_to_next_provider
                self._log_provider_failure(
                    provider_name=provider_name,
                    model=failure.get("model") or "",
                    attempt_number=attempt_number,
                    error=error,
                    classification=classification,
                    fallback_will_continue=fallback_will_continue,
                    stream=False,
                    duration_ms=provider_duration_ms,
                )

                if not classification.fallback_to_next_provider:
                    raise

                if overall_deadline_s is not None:
                    remaining_after_s = overall_deadline_s - (perf_counter() - overall_started_at)
                    if remaining_after_s <= 0.5:
                        logger.warning(
                            "llm_provider_deadline_exhausted | request_id=%s | after_provider=%s "
                            "| remaining_s=%.3f | attempted_providers=%s",
                            request_id(),
                            provider_name,
                            remaining_after_s,
                            ",".join(attempted_providers),
                        )
                        raise LLMFallbackError(
                            "LLM request deadline exhausted after provider failure.",
                            attempted_providers=list(attempted_providers),
                            failures=failures,
                        ) from last_error

                logger.info(
                    "llm_provider_fallback_continue | request_id=%s | from_provider=%s "
                    "| attempt=%s | category=%s",
                    request_id(),
                    provider_name,
                    attempt_number,
                    classification.category,
                )

        if not attempted_providers:
            logger.error(
                "llm_provider_exhausted | request_id=%s | attempted_providers= | reason=no_configured_providers",
                request_id(),
            )
            raise LLMFallbackError(
                "No configured LLM providers are available.",
                attempted_providers=[],
                failures=[],
            )

        logger.error(
            "llm_provider_exhausted | request_id=%s | attempted_providers=%s | attempts=%s",
            request_id(),
            ",".join(attempted_providers),
            len(attempted_providers),
        )
        raise LLMFallbackError(
            "All configured LLM providers failed. "
            f"Attempted providers: {', '.join(attempted_providers)}",
            attempted_providers=list(attempted_providers),
            failures=failures,
        ) from last_error

    def stream(self, request: LLMRequest) -> Iterator[LLMStreamChunk]:
        """Stream with fallback allowed only before first emitted content token."""

        attempted_providers: list[str] = []
        failures: list[dict[str, Any]] = []
        last_error: Exception | None = None
        overall_deadline_s = self._overall_deadline_seconds(request)
        overall_started_at = perf_counter()

        for provider_name in self.provider_priority:

            if provider_name not in _SUPPORTED_PROVIDERS:
                raise RuntimeError(
                    f"Unsupported provider in priority configuration: {provider_name}"
                )

            if not self.registry.is_configured(provider_name):
                logger.info(
                    "llm_provider_skipped_unconfigured | request_id=%s | provider=%s | stream=true",
                    request_id(),
                    provider_name,
                )
                continue

            remaining_s = None
            if overall_deadline_s is not None:
                remaining_s = overall_deadline_s - (perf_counter() - overall_started_at)
                if remaining_s <= 0.5:
                    logger.warning(
                        "llm_provider_deadline_exhausted | request_id=%s | before_provider=%s "
                        "| remaining_s=%.3f | attempted_providers=%s | stream=true",
                        request_id(),
                        provider_name,
                        remaining_s,
                        ",".join(attempted_providers),
                    )
                    raise LLMFallbackError(
                        "LLM request deadline exhausted before all providers could be attempted.",
                        attempted_providers=list(attempted_providers),
                        failures=failures,
                    ) from last_error

            attempt_request = (
                self._request_with_timeout(request, remaining_s)
                if remaining_s is not None
                else request
            )

            attempt_number = len(attempted_providers) + 1
            attempted_providers.append(provider_name)
            content_emitted = False
            provider: Any | None = None
            provider_model = ""

            try:
                provider = self.registry.get_provider(provider_name)
                provider_model = self._provider_model(provider_name, provider)
                self._log_provider_attempt(
                    provider_name=provider_name,
                    model=provider_model,
                    attempt_number=attempt_number,
                    stream=True,
                )

                provider_started_at = perf_counter()
                try:
                    for chunk in provider.stream(attempt_request):
                        if chunk.content:
                            content_emitted = True
                        yield chunk
                    provider_duration_ms = (
                        perf_counter() - provider_started_at
                    ) * 1000
                    record_timing("llm_provider", provider_duration_ms)
                    logger.info(
                        "llm_provider_stream_completed | request_id=%s | provider=%s | model=%s "
                        "| attempt=%s | duration_ms=%.2f",
                        request_id(),
                        provider_name,
                        provider_model or "unknown",
                        attempt_number,
                        provider_duration_ms,
                    )
                    return
                except Exception:
                    provider_duration_ms = (
                        perf_counter() - provider_started_at
                    ) * 1000
                    record_timing("llm_provider", provider_duration_ms)
                    raise

            except Exception as error:
                last_error = error
                classification = self.classify_provider_failure(error)
                provider_duration_ms = (
                    (perf_counter() - provider_started_at) * 1000
                    if "provider_started_at" in locals()
                    else None
                )
                try:
                    setattr(error, "_llm_fallback_classification", classification)
                    setattr(error, "_llm_fallback_provider", provider_name)
                except Exception:
                    pass
                failure = self._failure_record(
                    provider_name=provider_name,
                    model=provider_model or (self._provider_model(provider_name, provider) if provider else ""),
                    attempt_number=attempt_number,
                    error=error,
                    classification=classification,
                )
                failures.append(failure)

                fallback_will_continue = (
                    classification.fallback_to_next_provider and not content_emitted
                )
                self._log_provider_failure(
                    provider_name=provider_name,
                    model=failure.get("model") or "",
                    attempt_number=attempt_number,
                    error=error,
                    classification=classification,
                    fallback_will_continue=fallback_will_continue,
                    stream=True,
                    content_emitted=content_emitted,
                    duration_ms=provider_duration_ms,
                )

                # Critical safety rule: never restart/append a different provider after partial output.
                if content_emitted:
                    raise
                if not classification.fallback_to_next_provider:
                    raise

                if overall_deadline_s is not None:
                    remaining_after_s = overall_deadline_s - (perf_counter() - overall_started_at)
                    if remaining_after_s <= 0.5:
                        logger.warning(
                            "llm_provider_deadline_exhausted | request_id=%s | after_provider=%s "
                            "| remaining_s=%.3f | attempted_providers=%s | stream=true",
                            request_id(),
                            provider_name,
                            remaining_after_s,
                            ",".join(attempted_providers),
                        )
                        raise LLMFallbackError(
                            "LLM request deadline exhausted after provider failure.",
                            attempted_providers=list(attempted_providers),
                            failures=failures,
                        ) from last_error

                logger.info(
                    "llm_provider_fallback_continue | request_id=%s | from_provider=%s "
                    "| attempt=%s | category=%s | stream=true | pre_first_token=true",
                    request_id(),
                    provider_name,
                    attempt_number,
                    classification.category,
                )

        if not attempted_providers:
            logger.error(
                "llm_provider_exhausted | request_id=%s | attempted_providers= | stream=true | reason=no_configured_providers",
                request_id(),
            )
            raise LLMFallbackError(
                "No configured LLM providers are available.",
                attempted_providers=[],
                failures=[],
            )

        logger.error(
            "llm_provider_exhausted | request_id=%s | attempted_providers=%s | attempts=%s | stream=true",
            request_id(),
            ",".join(attempted_providers),
            len(attempted_providers),
        )
        raise LLMFallbackError(
            "All configured LLM providers failed. "
            f"Attempted providers: {', '.join(attempted_providers)}",
            attempted_providers=list(attempted_providers),
            failures=failures,
        ) from last_error
