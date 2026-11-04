import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from app.db.session import SessionLocal
from app.schemas.gmail_agent import (
    GetEmailPlan,
    GetThreadPlan,
    MeetingDiscoveryPlan,
    SearchEmailsPlan,
    SummarizeAttentionPlan,
    SummarizeImportantPlan,
    SummarizeLatestThreadPlan,
    SummarizeThreadPlan,
    SummarizeTodayPlan,
    SummarizeUnreadPlan,
    SummarizeUrgentPlan,
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
    terms: list[str] = [plan.query] if plan.query else []

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


def _search_queries(plan: SearchEmailsPlan) -> list[str]:
    constraints = []
    if plan.after:
        constraints.append(f"after:{plan.after:%Y/%m/%d}")
    if plan.before:
        constraints.append(f"before:{plan.before:%Y/%m/%d}")
    if plan.has_attachment is True:
        constraints.append("has:attachment")
    elif plan.has_attachment is False:
        constraints.append("-has:attachment")

    queries: list[str] = []
    phrase = plan.subject or plan.query
    if plan.sender:
        queries.append(f"from:{_quote_search_value(plan.sender)}")
        queries.append(f"subject:{_quote_search_value(plan.sender)}")
        partial_subject = " ".join(
            f"subject:{term}"
            for term in re.findall(r"[\w@.-]+", plan.sender)
        )
        if partial_subject:
            queries.append(partial_subject)
        queries.append(_quote_search_value(plan.sender))
    if phrase and not (plan.query and re.search(r"\b(?:has|filename|is|in):", plan.query)):
        queries.extend((
            f"subject:{_quote_search_value(phrase)}",
            " ".join(
                f"subject:{term}"
                for term in re.findall(r"[\w@.-]+", phrase)
            ),
            _quote_search_value(phrase),
            phrase,
        ))
    elif plan.query:
        queries.append(plan.query)

    unique_queries: list[str] = []
    for query in queries:
        combined = " ".join(part for part in (query, *constraints) if part)
        if combined and combined not in unique_queries:
            unique_queries.append(combined)
    if not unique_queries:
        unique_queries.append(build_gmail_search_query(plan))
    return unique_queries


def _search_metadata_matches(
    item: dict[str, Any],
    plan: SearchEmailsPlan,
) -> bool:
    headers = {
        "sender": str(item.get("sender") or "").casefold(),
        "subject": str(item.get("subject") or "").casefold(),
        "snippet": str(item.get("snippet") or "").casefold(),
        "body": str(item.get("body") or "").casefold(),
    }

    def contains_terms(text: str, phrase: str) -> bool:
        terms = [term.casefold() for term in re.findall(r"[\w@.-]+", phrase)]
        return bool(terms) and all(term in text for term in terms)

    if plan.subject and not contains_terms(headers["subject"], plan.subject):
        return False
    if plan.sender:
        if not any(
            contains_terms(headers[field], plan.sender)
            for field in ("sender", "subject", "snippet", "body")
        ):
            return False
    if plan.query and not re.search(
        r"\b(?:has|filename|is|in):",
        plan.query,
    ):
        if not any(
            contains_terms(headers[field], plan.query)
            for field in ("subject", "sender", "snippet", "body")
        ):
            return False
    return True


def _message_epoch(message: dict[str, Any]) -> float:
    internal_date = message.get("internal_date")
    try:
        if internal_date is not None:
            return float(internal_date) / 1000
    except (TypeError, ValueError):
        pass
    headers = message.get("headers") or {}
    date_value = headers.get("date") or message.get("date")
    if isinstance(date_value, str):
        try:
            parsed = parsedate_to_datetime(date_value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except (TypeError, ValueError, OverflowError):
            pass
    return 0


def _thread_timeline(
    messages: list[dict[str, Any]],
    account_email: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    timezone_kolkata = ZoneInfo("Asia/Kolkata")
    ordered = sorted(messages, key=_message_epoch)
    participants: dict[str, str] = {}
    timeline: list[dict[str, Any]] = []
    incoming_dates: list[float] = []
    outgoing_dates: list[float] = []

    for message in ordered:
        headers = message.get("headers") or {}
        sender = str(headers.get("from") or "")
        sender_pairs = getaddresses([sender])
        sender_address = sender_pairs[0][1].casefold() if sender_pairs else ""
        labels = message.get("labels")
        is_sent = isinstance(labels, list) and "SENT" in labels
        if is_sent or (
            account_email and sender_address == account_email.casefold()
        ):
            direction = "outgoing"
        elif account_email:
            direction = "incoming"
        else:
            direction = "unknown"
        if direction == "incoming":
            incoming_dates.append(_message_epoch(message))
        elif direction == "outgoing":
            outgoing_dates.append(_message_epoch(message))
        for header in ("from", "to", "cc"):
            for name, address in getaddresses([str(headers.get(header) or "")]):
                if address:
                    participants.setdefault(
                        address.casefold(),
                        name or address,
                    )

        epoch = _message_epoch(message)
        local_date = (
            datetime.fromtimestamp(epoch, timezone.utc)
            .astimezone(timezone_kolkata)
            .isoformat(timespec="minutes")
            if epoch
            else str(headers.get("date") or "Date unavailable")
        )
        compact = _compact_message(message)
        timeline.append({
            "direction": direction,
            "date_asia_kolkata": local_date,
            **compact,
        })

    latest_epoch = _message_epoch(ordered[-1]) if ordered else 0
    latest_incoming_epoch = max(incoming_dates, default=0)

    def local_date(epoch: float) -> str | None:
        if not epoch:
            return None
        return datetime.fromtimestamp(epoch, timezone.utc).astimezone(
            timezone_kolkata
        ).isoformat(timespec="minutes")

    subject = next(
        (
            str((message.get("headers") or {}).get("subject"))
            for message in ordered
            if (message.get("headers") or {}).get("subject")
        ),
        "",
    )
    return timeline, {
        "subject": subject,
        "participants": list(participants.values())[:30],
        "latest_message_date_asia_kolkata": local_date(latest_epoch),
        "latest_incoming_date_asia_kolkata": local_date(
            latest_incoming_epoch
        ),
        "latest_outgoing_date_asia_kolkata": local_date(
            max(outgoing_dates, default=0)
        ),
        "latest_message_direction": (
            timeline[-1]["direction"] if timeline else None
        ),
    }


def _compact_message(message: dict[str, Any]) -> dict[str, Any]:
    headers = message.get("headers") or {}
    body = message.get("body") or {}
    return {
        "sender": _bounded_text(headers.get("from"), 320),
        "recipient": _bounded_text(headers.get("to"), 500),
        "cc": _bounded_text(headers.get("cc"), 500),
        "subject": _bounded_text(headers.get("subject"), 500),
        "date": _bounded_text(headers.get("date"), 100),
        "internal_date": message.get("internal_date"),
        "snippet": _bounded_text(message.get("snippet"), 500),
        "labels": [
            label
            for label in message.get("labels", [])[:20]
            if isinstance(label, str)
        ] if isinstance(message.get("labels"), list) else [],
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
        "date": _bounded_text(
            headers.get("date") or message.get("date"),
            100,
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
    user_timezone: str = "UTC",
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
            user_timezone=user_timezone,
        )

    return _cap_result(result)


async def _execute_with_service(
    *,
    service: GmailService,
    intent: str,
    plan: dict[str, Any],
    user_id: UUID,
    connection_id: UUID,
    user_timezone: str = "UTC",
) -> dict[str, Any]:
    if intent == "discover_meetings":
        validated = MeetingDiscoveryPlan.model_validate_json(
            json.dumps(plan, ensure_ascii=False)
        )
        search_query = (
            "newer_than:180d "
            "{meeting interview appointment invitation scheduled}"
        )
        response = await service.list_messages(
            user_id=user_id,
            connection_id=connection_id,
            max_results=validated.max_results,
            query=search_query,
            folder="all",
        )
        candidates: list[dict[str, Any]] = []
        seen_threads: set[str] = set()
        for item in response.get("messages", []):
            if not isinstance(item, dict):
                continue
            thread_id = item.get("thread_id")
            if (
                isinstance(thread_id, str)
                and re.fullmatch(r"[A-Za-z0-9_-]{1,255}", thread_id)
                and thread_id not in seen_threads
            ):
                seen_threads.add(thread_id)
                candidates.append(item)
            if len(candidates) >= min(validated.max_results, 3):
                break

        semaphore = asyncio.Semaphore(3)

        async def fetch_meeting_thread(
            candidate: dict[str, Any],
        ) -> dict[str, Any]:
            thread_id = candidate["thread_id"]
            async with semaphore:
                thread = await service.get_thread(
                    user_id=user_id,
                    connection_id=connection_id,
                    thread_id=thread_id,
                    max_messages=10,
                    include_html=False,
                )
            raw_messages = thread.get("messages", [])
            if not isinstance(raw_messages, list):
                raw_messages = []
            if len(raw_messages) > 6:
                selected_messages = raw_messages[:3] + raw_messages[-3:]
            else:
                selected_messages = raw_messages
            messages = []
            for message in selected_messages:
                if not isinstance(message, dict):
                    continue
                compact = _compact_message(message)
                messages.append({
                    "message_id": message.get("id"),
                    "thread_id": message.get("thread_id") or thread_id,
                    "sender": compact["sender"],
                    "recipient": compact["recipient"],
                    "cc": compact["cc"],
                    "subject": compact["subject"],
                    "date": compact["date"],
                    "snippet": compact["snippet"][:300],
                    "body": compact["body"][:900],
                })
            return {
                "thread_id": thread_id,
                "truncated": bool(thread.get("truncated")),
                "messages": messages,
            }

        threads = await asyncio.gather(
            *(fetch_meeting_thread(candidate) for candidate in candidates)
        )
        source_messages = [
            message
            for thread in threads
            for message in thread["messages"]
        ]
        return {
            "intent": intent,
            "target_date": validated.target_date.isoformat(),
            "user_timezone": user_timezone,
            "search_criteria": search_query,
            "threads": threads,
            "gmail_sources": _gmail_sources(
                str(connection_id),
                source_messages,
            ),
        }

    if intent == "search_emails":
        validated = SearchEmailsPlan.model_validate_json(
            json.dumps(plan, ensure_ascii=False)
        )
        candidates: dict[str, dict[str, Any]] = {}
        search_criteria = _search_queries(validated)
        for query in search_criteria:
            response = await service.list_messages(
                user_id=user_id,
                connection_id=connection_id,
                max_results=validated.max_results,
                query=query,
                folder="all",
            )
            for item in response.get("messages", []):
                if (
                    isinstance(item, dict)
                    and isinstance(item.get("id"), str)
                ):
                    candidates.setdefault(item["id"], item)
        results: list[dict[str, Any]] = []
        detail_checks = 0
        for item in candidates.values():
            if _search_metadata_matches(item, validated):
                results.append(item)
            elif detail_checks < validated.max_results:
                detail_checks += 1
                detail = await service.get_message(
                    user_id=user_id,
                    connection_id=connection_id,
                    message_id=item["id"],
                    include_html=False,
                    max_body_chars=MAX_EMAIL_BODY_CHARS,
                )
                headers = detail.get("headers") or {}
                verified = {
                    **item,
                    "sender": headers.get("from") or item.get("sender"),
                    "subject": headers.get("subject") or item.get("subject"),
                    "snippet": detail.get("snippet") or item.get("snippet"),
                    "body": (detail.get("body") or {}).get("plain_text", ""),
                }
                if _search_metadata_matches(verified, validated):
                    results.append(item)
            if len(results) >= validated.max_results:
                break
        return {
            "intent": intent,
            "search_criteria": search_criteria,
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
                    "labels": [
                        label
                        for label in item.get("labels", [])[:20]
                        if isinstance(label, str)
                    ] if isinstance(item.get("labels"), list) else [],
                }
                for item in results
            ],
            "gmail_sources": _gmail_sources(
                str(connection_id),
                results,
            ),
            "result_size_estimate": len(results),
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
            include_html=False,
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
        messages = await _fetch_details(
            service,
            user_id,
            connection_id,
            unread.get("messages", []),
        )
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

    if intent == "summarize_important":
        validated = SummarizeImportantPlan.model_validate_json(
            json.dumps(plan, ensure_ascii=False),
        )
        query = "is:important newer_than:7d"
        inbox = await service.list_messages(
            user_id=user_id,
            connection_id=connection_id,
            max_results=validated.max_results,
            query=query,
            folder="inbox",
        )
        summaries = list(inbox.get("messages", []))
        seen_ids = {
            item.get("id")
            for item in summaries
            if isinstance(item, dict) and item.get("id")
        }
        remaining = validated.max_results - len(summaries)
        total_estimate = inbox.get("result_size_estimate", 0)
        if remaining > 0:
            other = await service.list_messages(
                user_id=user_id,
                connection_id=connection_id,
                max_results=remaining,
                query=f"{query} -in:inbox -in:spam -in:trash",
                folder="all",
            )
            total_estimate += other.get("result_size_estimate", 0)
            summaries.extend(
                item
                for item in other.get("messages", [])
                if isinstance(item, dict)
                and item.get("id") not in seen_ids
            )
        messages = await _fetch_details(
            service,
            user_id,
            connection_id,
            summaries[:validated.max_results],
        )
        return {
            "intent": intent,
            "period": "the last 7 days",
            "search_criteria": query,
            "inbox_prioritized": True,
            "messages": messages,
            "gmail_sources": _gmail_sources(
                str(connection_id),
                summaries[:validated.max_results],
            ),
            "result_size_estimate": total_estimate,
        }

    if intent in {"summarize_urgent", "summarize_attention"}:
        plan_model = (
            SummarizeUrgentPlan
            if intent == "summarize_urgent"
            else SummarizeAttentionPlan
        )
        validated = plan_model.model_validate_json(
            json.dumps(plan, ensure_ascii=False),
        )
        query = "{is:important is:unread} newer_than:7d"
        response = await service.list_messages(
            user_id=user_id,
            connection_id=connection_id,
            max_results=validated.max_results,
            query=query,
            folder="all",
        )
        summaries = [
            item
            for item in response.get("messages", [])
            if isinstance(item, dict)
        ]
        return {
            "intent": intent,
            "period": "the last 7 days",
            "search_criteria": query,
            "gmail_important_is_not_ai_urgency": True,
            "messages": await _fetch_details(
                service,
                user_id,
                connection_id,
                summaries,
            ),
            "gmail_sources": _gmail_sources(
                str(connection_id),
                summaries,
            ),
            "result_size_estimate": response.get(
                "result_size_estimate",
                0,
            ),
        }

    if intent == "summarize_today":
        validated = SummarizeTodayPlan.model_validate_json(
            json.dumps(plan, ensure_ascii=False),
        )
        timezone = ZoneInfo(user_timezone)
        today = datetime.now(timezone).date()
        start = datetime.combine(today, datetime.min.time(), tzinfo=timezone)
        end = start + timedelta(days=1)
        query = (
            f"after:{int(start.timestamp()) - 1} "
            f"before:{int(end.timestamp())} -from:me"
        )
        response = await service.list_messages(
            user_id=user_id,
            connection_id=connection_id,
            max_results=validated.max_results,
            query=query,
            folder="all",
        )
        summaries = [
            item
            for item in response.get("messages", [])
            if isinstance(item, dict)
        ]
        return {
            "intent": intent,
            "period": f"today ({today.isoformat()}, {user_timezone})",
            "search_criteria": query,
            "messages": [
                _compact_message(item)
                for item in summaries
            ],
            "gmail_sources": _gmail_sources(
                str(connection_id),
                summaries,
            ),
            "result_size_estimate": response.get(
                "result_size_estimate",
                0,
            ),
        }

    if intent == "summarize_latest_thread":
        validated = SummarizeLatestThreadPlan.model_validate_json(
            json.dumps(plan, ensure_ascii=False),
        )
        response = await service.list_messages(
            user_id=user_id,
            connection_id=connection_id,
            max_results=validated.max_results,
            query=validated.query,
            folder="all",
        )
        summaries = [
            item
            for item in response.get("messages", [])
            if isinstance(item, dict)
        ]
        latest = max(
            (
                item
                for item in summaries
                if isinstance(item.get("thread_id"), str)
                and item["thread_id"]
            ),
            key=_message_epoch,
            default=None,
        )
        if latest is None:
            return {
                "intent": intent,
                "query": validated.query,
                "messages": [],
                "gmail_sources": _gmail_sources(
                    str(connection_id),
                    summaries,
                ),
                "result_size_estimate": response.get(
                    "result_size_estimate",
                    0,
                ),
            }
        thread = await service.get_thread(
            user_id=user_id,
            connection_id=connection_id,
            thread_id=latest["thread_id"],
            max_messages=10,
            include_html=False,
        )
        connection = service._validate_connection(
            user_id=user_id,
            connection_id=connection_id,
        )
        timeline, thread_summary = _thread_timeline(
            [
                item
                for item in thread.get("messages", [])
                if isinstance(item, dict)
            ],
            str(getattr(connection, "email", "") or ""),
        )
        return {
            "intent": intent,
            "query": validated.query,
            "latest_match": {
                key: latest.get(key)
                for key in ("sender", "subject", "date")
            },
            "truncated": thread.get("truncated", False),
            **thread_summary,
            "messages": timeline,
            "gmail_sources": _gmail_sources(
                str(connection_id),
                thread.get("messages", []),
            ),
        }

    raise ValueError("Unsupported Gmail read intent.")


async def _fetch_details(
    service: GmailService,
    user_id: UUID,
    connection_id: UUID,
    summaries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    message_ids = [
        item["id"]
        for item in summaries[:10]
        if isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and item["id"]
    ]
    details = await service.get_messages_for_summary(
        user_id=user_id,
        connection_id=connection_id,
        message_ids=message_ids,
    )
    return [_compact_message(detail) for detail in details]
