import json
import re
from typing import Any
from uuid import UUID

from app.db.session import SessionLocal
from app.schemas.gmail_agent import (
    GetEmailPlan,
    GetThreadPlan,
    SearchEmailsPlan,
    SummarizeThreadPlan,
    SummarizeUnreadPlan,
)
from app.services.gmail_service import GmailService


MAX_EMAIL_BODY_CHARS = 3_000
MAX_SEARCH_TEXT_CHARS = 500
MAX_AGENT_RESULT_CHARS = 24_000
MAX_SOURCE_MESSAGES = 10


def _bounded_text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return value[:limit]


def _quote_search_value(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def build_gmail_search_query(plan: SearchEmailsPlan) -> str:
    terms: list[str] = []

    if plan.sender:
        terms.append(f"from:{_quote_search_value(plan.sender)}")
    if plan.subject:
        terms.append(f"subject:{_quote_search_value(plan.subject)}")
    if plan.after:
        terms.append(f"after:{plan.after:%Y/%m/%d}")
    if plan.before:
        terms.append(f"before:{plan.before:%Y/%m/%d}")
    if plan.has_attachment is True:
        terms.append("has:attachment")
    elif plan.has_attachment is False:
        terms.append("-has:attachment")

    return " ".join(terms)


def _compact_message(message: dict[str, Any]) -> dict[str, Any]:
    headers = message.get("headers") or {}
    body = message.get("body") or {}
    return {
        "sender": _bounded_text(headers.get("from"), 320),
        "recipient": _bounded_text(headers.get("to"), 500),
        "cc": _bounded_text(headers.get("cc"), 500),
        "subject": _bounded_text(headers.get("subject"), 500),
        "date": _bounded_text(headers.get("date"), 100),
        "snippet": _bounded_text(message.get("snippet"), 500),
        "body": _bounded_text(
            body.get("plain_text"),
            MAX_EMAIL_BODY_CHARS,
        ),
    }


def _source_record(message: dict[str, Any]) -> dict[str, str] | None:
    headers = message.get("headers") or {}
    message_id = message.get("id")
    thread_id = message.get("thread_id") or message.get("threadId")
    if not isinstance(message_id, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{1,255}",
        message_id,
    ):
        message_id = ""
    if not isinstance(thread_id, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{1,255}",
        thread_id,
    ):
        thread_id = ""
    if not message_id and not thread_id:
        return None

    return {
        "message_id": message_id,
        "thread_id": thread_id,
        "sender": _bounded_text(
            headers.get("from") or message.get("sender"),
            320,
        ),
        "recipient": _bounded_text(
            headers.get("to") or message.get("recipient"),
            500,
        ),
        "cc": _bounded_text(headers.get("cc") or message.get("cc"), 500),
        "subject": _bounded_text(
            headers.get("subject") or message.get("subject"),
            500,
        ),
    }


def _gmail_sources(
    connection_id: str,
    messages: list[dict[str, Any]],
) -> dict[str, Any]:
    records = [
        record
        for item in messages[:MAX_SOURCE_MESSAGES]
        if isinstance(item, dict)
        if (record := _source_record(item)) is not None
    ]
    return {
        "connection_id": connection_id,
        "messages": records,
    }


def _cap_result(result: dict[str, Any]) -> dict[str, Any]:
    if len(str(result)) <= MAX_AGENT_RESULT_CHARS:
        return result

    messages = result.get("messages")
    if isinstance(messages, list):
        capped_messages = []
        remaining = MAX_AGENT_RESULT_CHARS
        for message in messages:
            if not isinstance(message, dict) or remaining <= 0:
                break
            bounded = dict(message)
            for key, value in bounded.items():
                if isinstance(value, str):
                    bounded[key] = value[: min(len(value), remaining)]
                    remaining -= len(bounded[key])
            capped_messages.append(bounded)
        return {
            **result,
            "messages": capped_messages,
            "content_truncated": True,
        }

    return {
        "intent": result.get("intent", ""),
        "content_truncated": True,
    }


async def execute_gmail_read(
    *,
    intent: str,
    plan: dict[str, Any],
    user_id: str,
    connection_id: str,
) -> dict[str, Any]:
    """Execute one validated read-only Gmail plan via GmailService."""
    user_uuid = UUID(user_id)
    connection_uuid = UUID(connection_id)

    with SessionLocal() as db:
        service = GmailService(db)
        result = await _execute_with_service(
            service=service,
            intent=intent,
            plan=plan,
            user_id=user_uuid,
            connection_id=connection_uuid,
        )

    return _cap_result(result)


async def _execute_with_service(
    *,
    service: GmailService,
    intent: str,
    plan: dict[str, Any],
    user_id: UUID,
    connection_id: UUID,
) -> dict[str, Any]:
    if intent == "search_emails":
        validated = SearchEmailsPlan.model_validate_json(
            json.dumps(plan, ensure_ascii=False)
        )
        response = await service.list_messages(
            user_id=user_id,
            connection_id=connection_id,
            max_results=validated.max_results,
            query=build_gmail_search_query(validated),
            folder="all",
        )
        return {
            "intent": intent,
            "messages": [
                {
                    "sender": _bounded_text(item.get("sender"), 320),
                    "recipient": _bounded_text(item.get("recipient"), 500),
                    "subject": _bounded_text(item.get("subject"), 500),
                    "date": _bounded_text(item.get("date"), 100),
                    "snippet": _bounded_text(
                        item.get("snippet"),
                        MAX_SEARCH_TEXT_CHARS,
                    ),
                }
                for item in response.get("messages", [])
                if isinstance(item, dict)
            ],
            "gmail_sources": _gmail_sources(
                str(connection_id),
                response.get("messages", []),
            ),
            "result_size_estimate": response.get(
                "result_size_estimate",
                0,
            ),
        }

    if intent == "get_email":
        validated = GetEmailPlan.model_validate_json(
            json.dumps(plan, ensure_ascii=False)
        )
        response = await service.get_message(
            user_id=user_id,
            connection_id=connection_id,
            message_id=validated.message_id,
        )
        return {
            "intent": intent,
            "message": _compact_message(response),
            "gmail_sources": _gmail_sources(
                str(connection_id),
                [response],
            ),
        }

    if intent in {"get_thread", "summarize_thread"}:
        validated_thread = (
            GetThreadPlan.model_validate_json(
                json.dumps(plan, ensure_ascii=False)
            )
            if intent == "get_thread"
            else SummarizeThreadPlan.model_validate_json(
                json.dumps(plan, ensure_ascii=False)
            )
        )
        response = await service.get_thread(
            user_id=user_id,
            connection_id=connection_id,
            thread_id=validated_thread.thread_id,
            max_messages=10,
        )
        return {
            "intent": intent,
            "truncated": response.get("truncated", False),
            "messages": [
                _compact_message(message)
                for message in response.get("messages", [])
                if isinstance(message, dict)
            ],
            "gmail_sources": _gmail_sources(
                str(connection_id),
                response.get("messages", []),
            ),
        }

    if intent == "summarize_unread":
        validated = SummarizeUnreadPlan.model_validate_json(
            json.dumps(plan, ensure_ascii=False)
        )
        unread = await service.list_messages(
            user_id=user_id,
            connection_id=connection_id,
            max_results=validated.max_results,
            query="is:unread",
            folder="inbox",
        )
        messages = []
        for item in unread.get("messages", []):
            if not isinstance(item, dict) or not item.get("id"):
                continue
            detail = await service.get_message(
                user_id=user_id,
                connection_id=connection_id,
                message_id=item["id"],
            )
            messages.append(_compact_message(detail))
        return {
            "intent": intent,
            "messages": messages,
            "gmail_sources": _gmail_sources(
                str(connection_id),
                unread.get("messages", []),
            ),
            "result_size_estimate": unread.get(
                "result_size_estimate",
                0,
            ),
        }

    raise ValueError("Unsupported Gmail read intent.")
