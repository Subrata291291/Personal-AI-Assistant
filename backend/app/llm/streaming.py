"""Helpers for genuine LLM token streaming inside LangGraph nodes.

The provider adapters and ``FallbackManager.stream`` already implement real
streaming. This module bridges those streams into LangGraph's custom stream
writer so that tokens reach the HTTP SSE layer as they are generated.

Streaming is opt-in per execution via :func:`streaming_mode`. When streaming is
not enabled, callers keep using the existing blocking ``llm_router.generate``
path, preserving all non-streaming behaviour and tests.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from time import perf_counter
from typing import Any, Iterator

from app.llm.schemas import LLMRequest, LLMResponse


logger = logging.getLogger(__name__)

_STREAMING_ENABLED: ContextVar[bool] = ContextVar(
    "llm_streaming_enabled",
    default=False,
)


@contextmanager
def streaming_mode() -> Iterator[None]:
    """Enable node-level token streaming for the current execution context.

    The ContextVar token is bound to the context that created it, so this
    manager must not span a ``yield``/``await`` that may be resumed from a
    different context. Callers that advance a generator should enter the
    manager around each individual advance.
    """
    token = _STREAMING_ENABLED.set(True)
    try:
        yield
    finally:
        _STREAMING_ENABLED.reset(token)


def streaming_enabled() -> bool:
    return _STREAMING_ENABLED.get()


def _get_writer():
    """Return LangGraph's custom stream writer, or None outside a graph run."""
    try:
        from langgraph.config import get_stream_writer

        return get_stream_writer()
    except Exception:
        return None


def _safe_text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return value[:limit]


def emit_status(message: str) -> None:
    """Emit a short, non-sensitive progress status to the stream consumer."""
    writer = _get_writer()
    text = _safe_text(message, 200).replace("\n", " ").strip()
    if writer is not None and text:
        writer({"type": "status", "message": text})


def emit_replace(content: str) -> None:
    """Tell the client to replace already-streamed text with final content.

    Used only when post-generation validation changes the final response (for
    example the unverified-Calendar-claim guard). The persisted message and the
    rendered message therefore remain identical.
    """
    writer = _get_writer()
    text = _safe_text(content, 50000)
    if writer is not None and text:
        writer({"type": "replace", "content": text})


def generate_or_stream(request: LLMRequest) -> LLMResponse:
    """Generate a response, streaming tokens when an SSE consumer is active.

    Fallback semantics are delegated to ``llm_router``/``FallbackManager``:
    providers may be retried before the first content token, and never after
    partial output has been emitted.
    """
    if not streaming_enabled():
        from app.llm.router import llm_router

        return llm_router.generate(request)

    writer = _get_writer()
    if writer is None:
        from app.llm.router import llm_router

        return llm_router.generate(request)

    return _stream_response(request, writer)


def _stream_response(request: LLMRequest, writer) -> LLMResponse:
    from app.llm.router import llm_router

    started_at = perf_counter()
    parts: list[str] = []
    final_chunk = None
    provider = ""
    model = ""

    emit_status("Preparing your response...")

    for chunk in llm_router.stream(request):
        provider = chunk.provider or provider
        model = chunk.model or model
        if chunk.content:
            parts.append(chunk.content)
            writer({"type": "token", "content": chunk.content})
        if chunk.is_final:
            final_chunk = chunk

    content = "".join(parts)
    if not content.strip():
        # Empty provider output is a provider failure; the fallback manager has
        # already exhausted eligible providers at this point.
        raise RuntimeError("LLM provider returned an empty streamed response.")

    return LLMResponse(
        content=content,
        provider=provider or "unknown",
        model=model or "unknown",
        latency_ms=(perf_counter() - started_at) * 1000,
        usage=final_chunk.usage if final_chunk is not None else None,
        finish_reason=(
            final_chunk.finish_reason if final_chunk is not None else None
        ),
    )
