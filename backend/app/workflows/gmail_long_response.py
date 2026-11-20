"""Deterministic helpers for bounded, grounded long Gmail responses.

This module intentionally contains no provider or LangGraph calls. It turns
authoritative Gmail message records into verified intermediate summaries and a
bounded final answer so one oversized LLM request can never decide whether a
long response is complete.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

LONG_SUMMARY_MIN_MESSAGES = 3
SUMMARY_BATCH_SIZE = 5
SUMMARY_BATCH_MAX_TOKENS = 900
MAX_RENDER_SECTION_SIZE = 5
MAX_DETERMINISTIC_SUMMARY_CHARS = 260

COMPLETENESS_STATUSES = frozenset({"complete", "partial", "failed", "cancelled"})


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def normalize_summary_message(message: dict[str, Any]) -> dict[str, Any]:
    """Copy only authoritative fields from a retrieved Gmail message."""
    if not isinstance(message, dict):
        message = {}
    body = message.get("body")
    body_text = body if isinstance(body, str) else ""
    if not body_text and isinstance(body, dict):
        body_text = _text(body.get("plain_text") or body.get("html"))
    return {
        "message_id": _text(message.get("message_id") or message.get("id")),
        "thread_id": _text(
            message.get("thread_id")
            or message.get("threadId")
            or ""
        ),
        "sender": _text(
            message.get("sender")
            or (message.get("headers") or {}).get("from")
            or ""
        ),
        "recipient": _text(
            message.get("recipient")
            or (message.get("headers") or {}).get("to")
            or ""
        ),
        "subject": _text(
            message.get("subject")
            or (message.get("headers") or {}).get("subject")
            or ""
        ),
        "date": _text(
            message.get("date")
            or (message.get("headers") or {}).get("date")
            or ""
        ),
        "snippet": _text(message.get("snippet") or ""),
        "body": body_text,
        "body_truncated": bool(
            message.get("body_truncated")
            or (isinstance(body, dict) and body.get("truncated"))
        ),
        "attachments": [
            attachment
            for attachment in (message.get("attachments") or [])
            if isinstance(attachment, dict)
        ] if isinstance(message.get("attachments"), list) else [],
        "labels": [
            label
            for label in (message.get("labels") or [])
            if isinstance(label, str)
        ] if isinstance(message.get("labels"), list) else [],
    }


def prepare_summary_messages(
    messages: list[Any] | None,
) -> tuple[list[dict[str, Any]], int, int]:
    """Deduplicate retrieved messages by Gmail message ID.

    Returns the normalized unique messages, duplicate count, and invalid count.
    Missing IDs are treated as invalid; no identifier is ever invented.
    """
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    duplicates = 0
    invalid = 0
    for item in messages or []:
        normalized = normalize_summary_message(item)
        message_id = normalized["message_id"]
        if not message_id:
            invalid += 1
            continue
        if message_id in seen:
            duplicates += 1
            continue
        seen.add(message_id)
        unique.append(normalized)
    return unique, duplicates, invalid


def chunk_messages(
    messages: list[dict[str, Any]],
    size: int = SUMMARY_BATCH_SIZE,
) -> list[list[dict[str, Any]]]:
    if size <= 0:
        raise ValueError("Batch size must be positive.")
    return [messages[index:index + size] for index in range(0, len(messages), size)]


def build_completeness_metadata(
    *,
    requested_count: int,
    retrieved_count: int,
    processed_count: int,
    failed_count: int,
    remaining_count: int,
    output_complete: bool,
    grounding_verified: bool,
    output_limited: bool = False,
    duplicate_count: int = 0,
    finish_reason: str | None = None,
    usage: dict[str, Any] | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """Build one explicit completeness model for the response."""
    if status is not None and status not in COMPLETENESS_STATUSES:
        raise ValueError(f"Unsupported completeness status: {status}")
    if status is None:
        if processed_count == 0 and failed_count > 0:
            status = "failed"
        elif not output_complete or output_limited:
            status = "partial"
        elif (
            remaining_count > 0
            or failed_count > 0
            or retrieved_count < requested_count
            or not grounding_verified
        ):
            status = "partial"
        else:
            status = "complete"

    return {
        "status": status,
        "requested_item_count": requested_count,
        "retrieved_item_count": retrieved_count,
        "processed_item_count": processed_count,
        "failed_item_count": failed_count,
        "remaining_item_count": remaining_count,
        "duplicate_item_count": duplicate_count,
        "output_complete": output_complete,
        "output_limited": output_limited,
        "grounding_verified": grounding_verified,
        "finish_reason": finish_reason,
        "usage": usage,
    }


def merge_usage(responses: list[Any]) -> dict[str, Any] | None:
    totals: dict[str, int] = {}
    saw_usage = False
    for response in responses:
        usage = getattr(response, "usage", None)
        if usage is None:
            continue
        saw_usage = True
        data = (
            usage.model_dump(exclude_none=True)
            if hasattr(usage, "model_dump")
            else {}
        )
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = data.get(key)
            if isinstance(value, int):
                totals[key] = totals.get(key, 0) + value
    return totals or ({} if saw_usage else None)


def deterministic_email_summary(message: dict[str, Any]) -> str:
    """Build a conservative summary using only retrieved text."""
    text = re.sub(r"\s+", " ", message.get("body") or "").strip()
    if not text:
        text = re.sub(r"\s+", " ", message.get("snippet") or "").strip()
    if not text:
        return "No message body was included in the retrieved data."
    if len(text) > MAX_DETERMINISTIC_SUMMARY_CHARS:
        clipped = text[:MAX_DETERMINISTIC_SUMMARY_CHARS]
        boundary = clipped.rfind(" ")
        if boundary > 80:
            clipped = clipped[:boundary]
        text = clipped.rstrip(" ,;:") + "..."
    return f"Retrieved content: {text}"


def extract_verified_batch_summaries(
    content: str,
    batch: list[dict[str, Any]],
    validator: Callable[[str, dict[str, Any]], bool],
) -> dict[str, dict[str, str]]:
    """Parse a batch LLM response, keeping only verified per-message entries."""
    if not isinstance(content, str) or not content.strip():
        return {}

    raw = content.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    items = parsed.get("summaries") if isinstance(parsed, dict) else parsed
    if not isinstance(items, list):
        return {}

    messages_by_id = {
        message["message_id"]: message
        for message in batch
        if isinstance(message, dict) and message.get("message_id")
    }
    verified: dict[str, dict[str, str]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        message_id = _text(item.get("message_id"))
        message = messages_by_id.get(message_id)
        if message is None or message_id in verified:
            continue
        summary = _text(item.get("summary") or item.get("key_points")).strip()
        if not summary:
            continue
        # Validate each entry against its own authoritative message only. A
        # single batch-wide validation would allow facts from one email to
        # leak into another email's summary.
        item_payload = json.dumps(
            {"summaries": [item]},
            ensure_ascii=False,
        )
        if not validator(item_payload, {"messages": [message]}):
            continue
        verified[message_id] = {
            "summary": summary[:800],
            "action": _text(item.get("action") or "None stated").strip()[:300],
            "deadline": _text(item.get("deadline") or "Not specified").strip()[:200],
        }
    return verified


def _claim_or_fallback(value: str, fallback: str = "Not available") -> str:
    cleaned = re.sub(r"\s+", " ", value).strip()
    return cleaned or fallback


def render_grounded_email(
    message: dict[str, Any],
    index: int,
    verified: dict[str, str] | None = None,
) -> str:
    """Render one email card from authoritative fields and verified summary."""
    verified = verified or {}
    subject = _claim_or_fallback(message.get("subject"), "Not available")
    sender = _claim_or_fallback(message.get("sender"), "Not available")
    date = _claim_or_fallback(message.get("date"), "Not available")
    summary = verified.get("summary") or deterministic_email_summary(message)
    action = _claim_or_fallback(verified.get("action") or "None stated", "None stated")
    deadline = _claim_or_fallback(
        verified.get("deadline") or "Not specified",
        "Not specified",
    )
    return "\n".join([
        f"#### {index}. {subject}",
        f"- **From:** {sender}",
        f"- **Date:** {date}",
        f"- **Summary:** {summary}",
        f"- **Action required:** {action}",
        f"- **Deadline:** {deadline}",
    ])


def render_grounded_final_answer(
    messages: list[dict[str, Any]],
    *,
    verified_summaries: dict[str, dict[str, str]],
    completeness: dict[str, Any],
    period: str | None,
    search_criteria: str | list[str] | None,
) -> str:
    """Assemble bounded sections; every card maps to a retrieved message ID."""
    lines = ["## Email Summary"]
    if period:
        lines.append(f"**Period:** {period}")
    lines.append(
        f"**Emails requested:** {completeness['requested_item_count']}"
    )
    lines.append(
        f"**Emails retrieved:** {completeness['retrieved_item_count']}"
    )
    lines.append(
        f"**Emails summarized:** {completeness['processed_item_count']}"
    )
    lines.append(
        f"**Failed or unverified:** {completeness['failed_item_count']}"
    )
    lines.append(
        f"**Remaining:** {completeness['remaining_item_count']}"
    )
    if search_criteria:
        criteria_text = (
            ", ".join(search_criteria)
            if isinstance(search_criteria, list)
            else str(search_criteria)
        )
        lines.append(f"**Search criteria:** {criteria_text}")

    status = completeness["status"]
    if status != "complete":
        lines.append(
            f"**Completion status:** {status}. "
            "Counts above reflect only retrieved and verified messages."
        )
    if completeness.get("output_limited"):
        lines.append(
            "**Output status:** A bounded batch reached its output limit; "
            "verified fallback text was used for that batch."
        )
    if completeness.get("remaining_item_count", 0) > 0:
        lines.append(
            "**Coverage note:** The bounded request did not include every "
            "matching message; the remaining count is shown above."
        )

    for offset in range(0, len(messages), MAX_RENDER_SECTION_SIZE):
        section = messages[offset:offset + MAX_RENDER_SECTION_SIZE]
        lines.append(
            f"### Emails {offset + 1} through {offset + len(section)}"
        )
        for index, message in enumerate(section, start=offset + 1):
            lines.append(
                render_grounded_email(
                    message,
                    index,
                    verified_summaries.get(message["message_id"]),
                )
            )
    return "\n\n".join(lines)
