"""Minimal structured, redacted request-lifecycle diagnostics.

Uses existing application logging conventions only. Never logs secrets, tokens,
email/phone/contact/calendar/message/document contents, raw provider payloads,
or raw user messages.
"""

from __future__ import annotations

import hashlib
import logging
import time
from contextlib import contextmanager
from typing import Any, Mapping

from app.core.request_metrics import request_id as current_request_id


_diag_logger = logging.getLogger("app.diagnostics")


def _safe_id(value: object | None) -> str | None:
    if value is None:
        return None
    text = str(value)
    if not text:
        return None
    # Stable privacy-safe form (not reversible in logs).
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _sanitize_fields(fields: Mapping[str, Any] | None) -> dict[str, Any]:
    if not fields:
        return {}
    out: dict[str, Any] = {}
    for key, value in fields.items():
        # Drop any field that could carry sensitive/raw content by name.
        lowered = key.lower()
        if any(
            token in lowered
            for token in (
                "token",
                "secret",
                "password",
                "api_key",
                "apikey",
                "authorization",
                "cookie",
                "email_body",
                "body",
                "content",
                "message",
                "prompt",
                "description",
                "summary_text",
                "phone",
                "email",
                "contact",
                "payload_raw",
                "raw",
            )
        ):
            # Allow explicit safe booleans/counts/ids already sanitized by caller naming.
            if lowered in {
                "message_length",
                "raw_user_message_logged",
                "content_present",
            }:
                out[key] = value
            continue
        out[key] = value
    return out


def lifecycle_event(
    event: str,
    *,
    level: int = logging.INFO,
    user_id: object | None = None,
    conversation_id: object | None = None,
    request_id: object | None = None,
    **fields: Any,
) -> None:
    """Emit one structured lifecycle event with redacted/sanitized fields."""
    payload: dict[str, Any] = {
        "event": event,
        "request_id": str(request_id) if request_id is not None else current_request_id(),
        "user_ref": _safe_id(user_id),
        "conversation_ref": _safe_id(conversation_id),
    }
    payload.update(_sanitize_fields(fields))
    # Stable key=value rendering via existing logging conventions.
    parts = [f"diag_event={event}"]
    for k in sorted(payload.keys()):
        v = payload[k]
        if v is None:
            continue
        parts.append(f"{k}={v}")
    _diag_logger.log(level, " | ".join(parts))


@contextmanager
def timed_stage(event: str, **fields: Any):
    started = time.perf_counter()
    outcome = "ok"
    extra: dict[str, Any] = {}
    try:
        yield extra
    except Exception as exc:
        outcome = "error"
        extra["error_type"] = type(exc).__name__
        raise
    finally:
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        lifecycle_event(
            event,
            duration_ms=duration_ms,
            outcome=outcome,
            **fields,
            **extra,
        )
