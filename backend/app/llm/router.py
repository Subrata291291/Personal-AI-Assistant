
import logging
from collections.abc import Iterator
from time import perf_counter

from app.core.request_metrics import record_timing, request_id
from app.llm.fallback import FallbackManager
from app.llm.schemas import LLMRequest, LLMResponse, LLMStreamChunk


logger = logging.getLogger(__name__)


class LLMRouter:

    def __init__(self, fallback_manager: FallbackManager | None = None):
        self.fallback_manager = (
            fallback_manager or FallbackManager()
        )

    def generate(self, request: LLMRequest) -> LLMResponse:
        started_at = perf_counter()
        logger.info(
            "llm_request_started | request_id=%s | messages=%s",
            request_id(),
            len(request.messages),
        )

        try:
            response = self.fallback_manager.generate(request)
        finally:
            duration_ms = (perf_counter() - started_at) * 1000
            record_timing("llm", duration_ms)

        logger.info(
            "llm_request_completed | request_id=%s | duration_ms=%.2f "
            "| provider=%s | model=%s | finish_reason=%s "
            "| completion_tokens=%s | prompt_tokens=%s | status=%s",
            request_id(),
            duration_ms,
            response.provider,
            response.model,
            response.finish_reason,
            getattr(getattr(response, "usage", None), "completion_tokens", None),
            getattr(getattr(response, "usage", None), "prompt_tokens", None),
            "output_limited" if response.output_limited else "success",
        )

        return response

    def stream(self, request: LLMRequest) -> Iterator[LLMStreamChunk]:
        started_at = perf_counter()
        logger.info(
            "llm_stream_started | request_id=%s | messages=%s",
            request_id(),
            len(request.messages),
        )
        try:
            yield from self.fallback_manager.stream(request)
        finally:
            duration_ms = (perf_counter() - started_at) * 1000
            record_timing("llm", duration_ms)
            logger.info(
                "llm_stream_ended | request_id=%s | duration_ms=%.2f",
                request_id(),
                duration_ms,
            )


llm_router = LLMRouter()
