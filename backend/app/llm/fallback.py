
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
        self.attempted_providers = attempted_providers or []
        self.failures = failures or []
        summary = self.failure_summary()
        full_message = f"{message} Failure summary: {summary}" if summary else message
        super().__init__(full_message)
        self.base_message = message

    def failure_summary(self) -> str:
        """Sanitized per-provider failure summary (no secrets, no stack traces)."""
        parts: list[str] = []
        for failure in self.failures:
            provider = failure.get("provider") or "unknown"
            model = failure.get("model") or "unknown"
            category = failure.get("category") or "unknown"
            status = failure.get("status_code")
            code = failure.get("error_code")
            retry_attempted = failure.get("retry_attempted", False)
            fallback_continued = failure.get("fallback_continued", False)
            parts.append(
                f"{provider}[model={model}, category={category}, status={status}, "
                f"error_code={code}, retry_attempted={retry_attempted}, "
                f"fallback_continued={fallback_continued}]"
            )
        return "; ".join(parts)


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

    @staticmethod
    def _safe_text_fragments(error: Exception) -> str:
        """Collect sanitized, non-secret text hints for local classification only."""
        parts: list[str] = []
        for attr in ("code", "type", "param"):
            value = getattr(error, attr, None)
            if isinstance(value, str) and value:
                parts.append(value)
        body = getattr(error, "body", None)
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict):
                for key in ("code", "type", "message"):
                    value = err.get(key)
                    if isinstance(value, str) and value:
                        parts.append(value)
            for key in ("code", "type", "message"):
                value = body.get(key)
                if isinstance(value, str) and value:
                    parts.append(value)
        try:
            parts.append(str(error))
        except Exception:
            pass
        # Keep local matching bounded; never log this aggregate.
        return " ".join(parts)[:2000].casefold()

    @classmethod
    def _is_billing_or_quota_exhaustion(
        cls, error: Exception, status_code: int | None, safe_error_code: str | None
    ) -> bool:
        """Detect exhausted credits/quota/billing failures (non-retryable for provider)."""
        code = (safe_error_code or "").casefold()
        text = cls._safe_text_fragments(error)
        explicit_codes = {
            "insufficient_quota",
            "insufficient_credit",
            "insufficient_credits",
            "billing_hard_limit_reached",
            "billing_not_active",
            "quota_exceeded",
            "account_deactivated",
        }
        if code in explicit_codes:
            return True
        markers = (
            "insufficient_quota",
            "insufficient quota",
            "insufficient credit",
            "insufficient credits",
            "no credits remaining",
            "no credit remaining",
            "credit balance is too low",
            "out of credits",
            "exceeded your current quota",
            "quota exceeded",
            "billing hard limit",
            "billing limit",
            "account deactivated",
            "plan and billing",
        )
        if any(marker in text for marker in markers):
            # Avoid misclassifying generic transient throttling without billing/quota signals.
            if status_code in {402, 429, 403, 400} or "quota" in text or "credit" in text or "billing" in text:
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

        # Billing/quota/credit exhaustion: permanent for this provider/account, not retried
        # on the same provider, but safe to try a different configured provider.
        # Keep 402 in its dedicated category for compatibility; classify explicit
        # insufficient-credits/quota exhaustion separately from transient 429 rate limits.
        if status_code != 402 and cls._is_billing_or_quota_exhaustion(
            error, status_code, safe_error_code
        ):
            return ProviderFailureClassification(
                fallback_to_next_provider=True,
                category="provider_billing_or_quota_exhausted",
                status_code=status_code,
                safe_error_code=safe_error_code or "billing_or_quota_exhausted",
                retry_same_provider=False,
            )

        # Provider/server/network failures: safe to try another configured provider.
        if status_code == 429:
            return ProviderFailureClassification(
                fallback_to_next_provider=True,
                category="rate_limit",
                status_code=status_code,
                safe_error_code=safe_error_code,
                retry_same_provider=False,
            )

        if status_code == 408 or (status_code is not None and 500 <= status_code <= 599):
            return ProviderFailureClassification(
                fallback_to_next_provider=True,
                category="provider_server_error" if status_code and status_code >= 500 else "timeout",
                status_code=status_code,
                safe_error_code=safe_error_code,
                retry_same_provider=False,
            )

        # Explicit provider/account/model/access failures: non-retryable for this provider,
        # but eligible to try a different configured provider.
        if status_code == 401:
            return ProviderFailureClassification(
                fallback_to_next_provider=True,
                category="provider_auth_error",
                status_code=status_code,
                safe_error_code=safe_error_code,
                retry_same_provider=False,
            )
        if status_code == 402:
            return ProviderFailureClassification(
                fallback_to_next_provider=True,
                category="provider_payment_or_quota_error",
                status_code=status_code,
                safe_error_code=safe_error_code,
                retry_same_provider=False,
            )
        if status_code == 403:
            return ProviderFailureClassification(
                fallback_to_next_provider=True,
                category="provider_access_or_policy_error",
                status_code=status_code,
                safe_error_code=safe_error_code,
                retry_same_provider=False,
            )
        if status_code == 404:
            return ProviderFailureClassification(
                fallback_to_next_provider=True,
                category="provider_model_or_resource_unavailable",
                status_code=status_code,
                safe_error_code=safe_error_code,
                retry_same_provider=False,
            )

        # Likely request/input issues (possibly shared across providers) or unknown 4xx:
        # do not automatically fall back.
        if status_code is not None and 400 <= status_code <= 499:
            return ProviderFailureClassification(
                fallback_to_next_provider=False,
                category="request_error_non_fallback",
                status_code=status_code,
                safe_error_code=safe_error_code,
                retry_same_provider=False,
            )

        if cls._is_network_or_timeout(error):
            return ProviderFailureClassification(
                fallback_to_next_provider=True,
                category="network_or_timeout",
                status_code=status_code,
                safe_error_code=safe_error_code,
                retry_same_provider=False,
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
                    "provider_billing_or_quota_exhausted",
                }:
                    return ProviderFailureClassification(
                        fallback_to_next_provider=True,
                        category=nested.category,
                        status_code=nested.status_code or status_code,
                        safe_error_code=nested.safe_error_code or safe_error_code,
                        retry_same_provider=nested.retry_same_provider,
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
                    retry_same_provider=False,
                )

        # Default: application/programming/unexpected errors must not be silently retried
        # across providers.
        return ProviderFailureClassification(
            fallback_to_next_provider=False,
            category="application_or_unexpected_error",
            status_code=status_code,
            safe_error_code=safe_error_code,
            retry_same_provider=False,
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
        retry_attempted: bool = False,
    ) -> None:
        logger.warning(
            "llm_provider_attempt_failed | request_id=%s | provider=%s | model=%s "
            "| attempt=%s | stream=%s | duration_ms=%s | exception_class=%s.%s | status=%s "
            "| error_code=%s | category=%s | fallback_to_next_provider=%s "
            "| retry_attempted=%s | fallback_continued=%s | fallback_will_continue=%s "
            "| fallback_initiated=%s | content_emitted=%s",
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
            retry_attempted,
            fallback_will_continue,
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
        retry_attempted: bool = False,
        fallback_continued: bool = False,
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
            "retry_attempted": retry_attempted,
            "fallback_continued": fallback_continued,
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

        for idx, provider_name in enumerate(self.provider_priority):

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

            has_next_configured_provider = any(
                candidate in _SUPPORTED_PROVIDERS and self.registry.is_configured(candidate)
                for candidate in self.provider_priority[idx + 1 :]
            )

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

                fallback_will_continue = (
                    classification.fallback_to_next_provider and has_next_configured_provider
                )
                if fallback_will_continue and overall_deadline_s is not None:
                    remaining_after_s = overall_deadline_s - (perf_counter() - overall_started_at)
                    if remaining_after_s <= 0.5:
                        fallback_will_continue = False

                failure = self._failure_record(
                    provider_name=provider_name,
                    model=provider_model or (self._provider_model(provider_name, provider) if provider else ""),
                    attempt_number=attempt_number,
                    error=error,
                    classification=classification,
                    retry_attempted=bool(classification.retry_same_provider),
                    fallback_continued=fallback_will_continue,
                )
                failures.append(failure)

                self._log_provider_failure(
                    provider_name=provider_name,
                    model=failure.get("model") or "",
                    attempt_number=attempt_number,
                    error=error,
                    classification=classification,
                    fallback_will_continue=fallback_will_continue,
                    stream=False,
                    duration_ms=provider_duration_ms,
                    retry_attempted=bool(classification.retry_same_provider),
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
                    "| attempt=%s | category=%s | retry_attempted=%s | fallback_continued=true",
                    request_id(),
                    provider_name,
                    attempt_number,
                    classification.category,
                    bool(classification.retry_same_provider),
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

        for idx, provider_name in enumerate(self.provider_priority):

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

            has_next_configured_provider = any(
                candidate in _SUPPORTED_PROVIDERS and self.registry.is_configured(candidate)
                for candidate in self.provider_priority[idx + 1 :]
            )

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
                content_chunks = 0
                try:
                    for chunk in provider.stream(attempt_request):
                        if chunk.content:
                            content_chunks += 1
                            content_emitted = True
                        yield chunk
                except Exception:
                    provider_duration_ms = (
                        perf_counter() - provider_started_at
                    ) * 1000
                    record_timing("llm_provider", provider_duration_ms)
                    raise

                provider_duration_ms = (
                    perf_counter() - provider_started_at
                ) * 1000
                record_timing("llm_provider", provider_duration_ms)

                # A stream that produced no content is an empty provider
                # response, not a successful generation. Classify it through
                # the same failure path as adapters that raise for empty
                # responses so fallback can happen before any token is emitted.
                if content_chunks == 0:
                    raise RuntimeError(
                        f"Provider {provider_name} returned an empty response."
                    )

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

                fallback_will_continue = (
                    classification.fallback_to_next_provider
                    and not content_emitted
                    and has_next_configured_provider
                )
                if fallback_will_continue and overall_deadline_s is not None:
                    remaining_after_s = overall_deadline_s - (perf_counter() - overall_started_at)
                    if remaining_after_s <= 0.5:
                        fallback_will_continue = False

                failure = self._failure_record(
                    provider_name=provider_name,
                    model=provider_model or (self._provider_model(provider_name, provider) if provider else ""),
                    attempt_number=attempt_number,
                    error=error,
                    classification=classification,
                    retry_attempted=bool(classification.retry_same_provider),
                    fallback_continued=fallback_will_continue,
                )
                failures.append(failure)

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
                    retry_attempted=bool(classification.retry_same_provider),
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
                    "| attempt=%s | category=%s | stream=true | pre_first_token=true "
                    "| retry_attempted=%s | fallback_continued=true",
                    request_id(),
                    provider_name,
                    attempt_number,
                    classification.category,
                    bool(classification.retry_same_provider),
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
