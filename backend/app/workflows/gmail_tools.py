import asyncio
import json
import logging
import re
from html.parser import HTMLParser
from datetime import datetime, timedelta, timezone
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.db.session import SessionLocal
from app.schemas.gmail_agent import (
    MAX_GMAIL_READ_RESULTS,
    GetEmailPlan,
    GetLatestEmailPlan,
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
MAX_LIST_PAGES = 5
PAGE_SIZE = 50

logger = logging.getLogger(__name__)


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._hidden_depth = 0

    def handle_data(self, data: str) -> None:
        if not self._hidden_depth and data.strip():
            self.parts.append(data.strip())

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() in {"script", "style", "template"}:
            self._hidden_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"script", "style", "template"} and self._hidden_depth:
            self._hidden_depth -= 1

    def get_text(self) -> str:
        return "\n".join(self.parts)


def _html_to_readable_text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    parser = _HTMLTextExtractor()
    parser.feed(value)
    parser.close()
    return parser.get_text()


def _bounded_text(value: Any, limit: int) -> str:
    return _bounded_text_with_flag(value, limit)[0]


def _bounded_text_with_flag(value: Any, limit: int) -> tuple[str, bool]:
    if not isinstance(value, str):
        return "", False
    return value[:limit], len(value) > limit


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


def build_important_search_query(
    plan: SummarizeImportantPlan,
    *,
    user_timezone: str = "UTC",
    request_time: Any = None,
) -> tuple[str, dict[str, Any]]:
    """Build one scope-aware IMPORTANT query from structured plan criteria."""
    zone_name = user_timezone or "UTC"
    try:
        zone = ZoneInfo(zone_name)
    except (ValueError, ZoneInfoNotFoundError):
        zone_name = "UTC"
        zone = ZoneInfo("UTC")

    now = _parse_request_time(request_time) or datetime.now(timezone.utc)
    today = now.astimezone(zone).date()
    period_label = plan.period_label
    start: datetime | None = None
    end: datetime | None = None

    if plan.period == "today":
        start = datetime.combine(today, datetime.min.time(), tzinfo=zone)
        end = start + timedelta(days=1)
        period_label = period_label or f"today ({today.isoformat()}, {zone_name})"
    elif plan.period == "yesterday":
        yesterday = today - timedelta(days=1)
        start = datetime.combine(yesterday, datetime.min.time(), tzinfo=zone)
        end = start + timedelta(days=1)
        period_label = period_label or (
            f"yesterday ({yesterday.isoformat()}, {zone_name})"
        )
    elif plan.period == "this_week":
        monday = today - timedelta(days=today.weekday())
        start = datetime.combine(monday, datetime.min.time(), tzinfo=zone)
        end = datetime.combine(today, datetime.min.time(), tzinfo=zone) + timedelta(
            days=1,
        )
        period_label = period_label or (
            f"this week ({monday.isoformat()} to {today.isoformat()}, {zone_name})"
        )
    elif plan.period == "last_n_days":
        window_days = plan.window_days or 7
        first_day = today - timedelta(days=window_days - 1)
        start = datetime.combine(first_day, datetime.min.time(), tzinfo=zone)
        end = datetime.combine(today, datetime.min.time(), tzinfo=zone) + timedelta(
            days=1,
        )
        period_label = period_label or (
            f"the last {window_days} days "
            f"({first_day.isoformat()} to {today.isoformat()}, {zone_name})"
        )
    elif plan.period == "custom":
        if plan.after:
            start = datetime.combine(plan.after, datetime.min.time(), tzinfo=zone)
        if plan.before:
            end = datetime.combine(plan.before, datetime.min.time(), tzinfo=zone)
        if plan.after and plan.before:
            period_label = period_label or (
                f"{plan.after.isoformat()} to {plan.before.isoformat()} ({zone_name})"
            )
        elif plan.after:
            period_label = period_label or (
                f"since {plan.after.isoformat()} ({zone_name})"
            )
        elif plan.before:
            period_label = period_label or (
                f"before {plan.before.isoformat()} ({zone_name})"
            )

    terms: list[str] = ["is:important"]
    if plan.read_status == "unread":
        terms.append("is:unread")
    elif plan.read_status == "read":
        terms.append("-is:unread")
    if plan.sender:
        terms.append(f"from:{_quote_search_value(plan.sender)}")
    if plan.recipient:
        terms.append(f"to:{_quote_search_value(plan.recipient)}")
    if plan.topic:
        terms.append(_quote_search_value(plan.topic))
    if start is not None:
        terms.append(f"after:{int(start.timestamp()) - 1}")
    if end is not None:
        terms.append(f"before:{int(end.timestamp())}")

    query = " ".join(terms)
    if len(query) > MAX_SEARCH_TEXT_CHARS:
        raise ValueError("Important-email query exceeds the Gmail search limit.")
    return query, {
        "folder": {
            "all": "all",
            "inbox": "inbox",
            "sent": "sent",
        }[plan.scope],
        "time_zone": zone_name,
        "period_label": period_label or "unspecified (all dates)",
        "start": start,
        "end": end,
        "criteria": {
            "importance": plan.importance_source,
            "read_status": plan.read_status,
            "scope": plan.scope,
            "period": plan.period,
            "period_label": period_label or "unspecified (all dates)",
            "window_days": plan.window_days,
            "sender": plan.sender,
            "recipient": plan.recipient,
            "topic": plan.topic,
        },
    }


# Gmail operator prefixes that must be interpreted by the API, not matched as
# literal text against sender/subject/body after the API returns candidates.
_GMAIL_STRUCTURED_QUERY_RE = re.compile(
    r"\b(?:has|filename|is|in|after|before|from|to|subject|"
    r"newer_than|older_than|label|category):",
    re.IGNORECASE,
)


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
    if phrase and not (plan.query and _GMAIL_STRUCTURED_QUERY_RE.search(plan.query)):
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
        parts = [part for part in (query, *constraints) if part]
        combined_probe = " ".join(parts).casefold()
        # Read-side searches are received-mail searches unless the caller
        # explicitly asked for the Sent mailbox.
        if (
            "in:sent" not in combined_probe
            and "from:me" not in combined_probe
            and "sent" not in combined_probe
        ):
            parts.append("-from:me")
        combined = " ".join(parts)
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
        if not contains_terms(headers["sender"], plan.sender):
            return False
    if plan.query and not _GMAIL_STRUCTURED_QUERY_RE.search(plan.query):
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


def _parse_request_time(value: Any) -> datetime | None:
    """Parse the injected chat request time without inventing a date."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            raw = value.strip()
            parsed = datetime.fromisoformat(
                raw[:-1] + "+00:00" if raw.endswith("Z") else raw
            )
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _internal_epoch(message: dict[str, Any]) -> float | None:
    """Return Gmail's authoritative internalDate in seconds, if usable."""
    raw = message.get("internal_date")
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if value != value or value <= 0 or value >= 1e18:
        return None
    return value / 1000


async def _collect_message_pages(
    *,
    service: GmailService,
    user_id: UUID,
    connection_id: UUID,
    query: str | None,
    folder: str,
    page_size: int = PAGE_SIZE,
    max_pages: int = MAX_LIST_PAGES,
) -> tuple[list[dict[str, Any]], int]:
    """Follow bounded Gmail pagination; never loop forever on a bad token."""
    collected: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    page_token: str | None = None
    total_estimate = 0
    for _ in range(max(1, max_pages)):
        response = await service.list_messages(
            user_id=user_id,
            connection_id=connection_id,
            max_results=page_size,
            page_token=page_token,
            query=query,
            folder=folder,
        )
        if not isinstance(response, dict):
            break
        for item in response.get("messages") or []:
            if not isinstance(item, dict):
                continue
            message_id = item.get("id")
            if not isinstance(message_id, str) or not message_id:
                continue
            if message_id in seen_ids:
                continue
            seen_ids.add(message_id)
            collected.append(item)
        try:
            total_estimate = max(
                total_estimate,
                int(response.get("result_size_estimate") or 0),
            )
        except (TypeError, ValueError):
            pass
        next_token = response.get("next_page_token")
        page_token = next_token if isinstance(next_token, str) and next_token else None
        if not page_token or len(collected) >= page_size * max_pages:
            break
    return collected, total_estimate


async def _collect_message_pages_bounded(
    *,
    service: GmailService,
    user_id: UUID,
    connection_id: UUID,
    query: str | None,
    folder: str,
    page_size: int = PAGE_SIZE,
    max_pages: int = MAX_LIST_PAGES,
    target_count: int | None = None,
) -> tuple[list[dict[str, Any]], int, bool]:
    """Follow Gmail pagination up to a bounded target and report remaining work.

    The legacy :func:`_collect_message_pages` intentionally returns only two
    values. Summary reads need to know whether a next page existed so the final
    response cannot claim completeness when Gmail still has matching mail.
    """
    collected: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    page_token: str | None = None
    total_estimate = 0
    has_more = False

    for _ in range(max(1, max_pages)):
        if target_count is not None and len(collected) >= target_count:
            has_more = True
            break
        response = await service.list_messages(
            user_id=user_id,
            connection_id=connection_id,
            max_results=page_size,
            page_token=page_token,
            query=query,
            folder=folder,
        )
        if not isinstance(response, dict):
            break
        for item in response.get("messages") or []:
            if not isinstance(item, dict):
                continue
            message_id = item.get("id")
            if not isinstance(message_id, str) or not message_id:
                continue
            if message_id in seen_ids:
                continue
            seen_ids.add(message_id)
            collected.append(item)
            if target_count is not None and len(collected) >= target_count:
                break
        try:
            total_estimate = max(
                total_estimate,
                int(response.get("result_size_estimate") or 0),
            )
        except (TypeError, ValueError):
            pass
        next_token = response.get("next_page_token")
        page_token = next_token if isinstance(next_token, str) and next_token else None
        if (
            target_count is not None
            and len(collected) >= target_count
        ):
            has_more = bool(page_token) or total_estimate > len(collected)
            break
        if not page_token or len(collected) >= page_size * max_pages:
            has_more = bool(page_token) or total_estimate > len(collected)
            break

    # The loop can exit by exhausting ``max_pages`` without entering either
    # break above. A live next-page token still means the result is partial.
    if page_token or total_estimate > len(collected):
        has_more = True

    return collected, total_estimate, has_more


def _sort_by_internal_date(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort authoritative metadata by internalDate; unknown timestamps go last."""
    return sorted(
        messages,
        key=lambda item: (
            _internal_epoch(item) is not None,
            _internal_epoch(item) or 0,
        ),
        reverse=True,
    )


def _thread_timeline(
    messages: list[dict[str, Any]],
    account_email: str,
    user_timezone: str = "UTC",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    zone_name = user_timezone or "UTC"
    try:
        presentation_zone = ZoneInfo(zone_name)
    except (ValueError, ZoneInfoNotFoundError):
        zone_name = "UTC"
        presentation_zone = ZoneInfo("UTC")
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
            .astimezone(presentation_zone)
            .isoformat(timespec="minutes")
            if epoch
            else str(headers.get("date") or "Date unavailable")
        )
        compact = _compact_message(message)
        timeline.append({
            "direction": direction,
            "date_local": local_date,
            "date_asia_kolkata": local_date,
            **compact,
        })

    latest_epoch = _message_epoch(ordered[-1]) if ordered else 0
    latest_incoming_epoch = max(incoming_dates, default=0)

    def local_date(epoch: float) -> str | None:
        if not epoch:
            return None
        return datetime.fromtimestamp(epoch, timezone.utc).astimezone(
            presentation_zone
        ).isoformat(timespec="minutes")

    subject = next(
        (
            str((message.get("headers") or {}).get("subject"))
            for message in ordered
            if (message.get("headers") or {}).get("subject")
        ),
        "",
    )
    latest_message_date = local_date(latest_epoch)
    latest_incoming_date = local_date(latest_incoming_epoch)
    latest_outgoing_date = local_date(max(outgoing_dates, default=0))
    return timeline, {
        "subject": subject,
        "participants": list(participants.values())[:30],
        "presentation_timezone": zone_name,
        "latest_message_date_local": latest_message_date,
        "latest_incoming_date_local": latest_incoming_date,
        "latest_outgoing_date_local": latest_outgoing_date,
        # Legacy aliases retained for clients that already consume these keys.
        "latest_message_date_asia_kolkata": latest_message_date,
        "latest_incoming_date_asia_kolkata": latest_incoming_date,
        "latest_outgoing_date_asia_kolkata": latest_outgoing_date,
        "latest_message_direction": (
            timeline[-1]["direction"] if timeline else None
        ),
    }


def _compact_attachments(message: dict[str, Any]) -> list[dict[str, Any]]:
    """Keep bounded attachment metadata so attachment claims can be verified."""
    attachments = message.get("attachments")
    if not isinstance(attachments, list):
        return []
    compact: list[dict[str, Any]] = []
    for item in attachments[:20]:
        if not isinstance(item, dict):
            continue
        compact.append({
            "filename": _bounded_text(item.get("filename"), 320),
            "mime_type": _bounded_text(item.get("mime_type"), 160),
            "attachment_id": _bounded_text(item.get("attachment_id"), 160),
            "size": item.get("size"),
            "is_inline": bool(item.get("is_inline")),
        })
    return compact


def _compact_message(message: dict[str, Any]) -> dict[str, Any]:
    headers = message.get("headers") or {}
    body = message.get("body") or {}
    plain_body, body_truncated = _bounded_text_with_flag(
        body.get("plain_text"),
        MAX_EMAIL_BODY_CHARS,
    )
    if not plain_body:
        html_body = _html_to_readable_text(body.get("html"))
        plain_body, html_truncated = _bounded_text_with_flag(
            html_body,
            MAX_EMAIL_BODY_CHARS,
        )
        body_truncated = body_truncated or html_truncated
    if isinstance(body, dict):
        body_truncated = body_truncated or bool(body.get("truncated"))
    body_truncated = body_truncated or bool(message.get("body_truncated"))
    # List-mode Gmail results expose flat sender/subject/date fields and no
    # headers/body map. Fall back to those authoritative top-level values so a
    # metadata-only result never loses its identity.
    return {
        "message_id": message.get("id") or message.get("message_id"),
        "thread_id": (
            message.get("thread_id") or message.get("threadId")
        ),
        "sender": _bounded_text(
            headers.get("from") or message.get("sender"),
            320,
        ),
        "recipient": _bounded_text(
            headers.get("to") or message.get("recipient"),
            500,
        ),
        "cc": _bounded_text(
            headers.get("cc") or message.get("cc"),
            500,
        ),
        "subject": _bounded_text(
            headers.get("subject") or message.get("subject"),
            500,
        ),
        "date": _bounded_text(
            headers.get("date") or message.get("date"),
            100,
        ),
        "internal_date": message.get("internal_date"),
        "snippet": _bounded_text(message.get("snippet"), 500),
        "labels": [
            label
            for label in message.get("labels", [])[:20]
            if isinstance(label, str)
        ] if isinstance(message.get("labels"), list) else [],
        "body": plain_body,
        "body_truncated": body_truncated,
        "attachments": _compact_attachments(message),
    }


def _source_record(message: dict[str, Any]) -> dict[str, str] | None:
    headers = message.get("headers") or {}
    message_id = message.get("id") or message.get("message_id")
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
        "internal_date": message.get("internal_date"),
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
        # Keep whole records. Truncating individual fields could corrupt a
        # message ID or split a factual claim; dropping whole records and
        # setting content_truncated keeps every displayed item authoritative.
        capped_messages = []
        remaining = MAX_AGENT_RESULT_CHARS
        for message in messages:
            if not isinstance(message, dict) or remaining <= 0:
                break
            message_size = len(str(message)) + 2
            if message_size > remaining:
                break
            capped_messages.append(dict(message))
            remaining -= message_size

        dropped_count = len(messages) - len(capped_messages)
        displayed_ids = {
            item.get("message_id")
            for item in capped_messages
            if isinstance(item.get("message_id"), str) and item["message_id"]
        }
        raw_retrieved = result.get("retrieved_count", len(messages))
        try:
            raw_retrieved = max(0, int(raw_retrieved))
        except (TypeError, ValueError):
            raw_retrieved = len(messages)
        raw_processed = result.get("processed_count", raw_retrieved)
        try:
            raw_processed = max(0, int(raw_processed))
        except (TypeError, ValueError):
            raw_processed = raw_retrieved
        raw_remaining = result.get("remaining_count", 0)
        try:
            raw_remaining = max(0, int(raw_remaining))
        except (TypeError, ValueError):
            raw_remaining = 0

        failed_ids = [
            message_id
            for message_id in (result.get("failed_message_ids") or [])
            if message_id not in displayed_ids
        ]
        sources = result.get("gmail_sources")
        if isinstance(sources, dict) and isinstance(sources.get("messages"), list):
            sources = {
                **sources,
                "messages": [
                    record
                    for record in sources["messages"]
                    if isinstance(record, dict)
                    and record.get("message_id") in displayed_ids
                ],
            }

        return {
            **result,
            "messages": capped_messages,
            "retrieved_count": len(capped_messages),
            "processed_count": min(raw_processed, len(capped_messages)),
            "retrieved_message_ids": [
                item["message_id"]
                for item in capped_messages
                if item.get("message_id")
            ],
            "failed_message_ids": failed_ids,
            "failed_count": len(failed_ids),
            "remaining_count": max(raw_remaining, dropped_count),
            "has_more": bool(
                result.get("has_more") or dropped_count > 0 or raw_remaining > 0
            ),
            "gmail_sources": sources,
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
    request_time: Any = None,
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
            request_time=request_time,
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
    request_time: Any = None,
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
        total_estimate = 0
        for query in search_criteria:
            items, estimate = await _collect_message_pages(
                service=service,
                user_id=user_id,
                connection_id=connection_id,
                query=query,
                folder="all",
            )
            total_estimate = max(total_estimate, estimate)
            for item in items:
                candidates.setdefault(item["id"], item)
        logger.info(
            "gmail_read_candidates | connection_id=%s | intent=%s | "
            "candidate_count=%d | query_count=%d",
            connection_id,
            intent,
            len(candidates),
            len(search_criteria),
        )
        ordered_candidates = _sort_by_internal_date(list(candidates.values()))
        results: list[dict[str, Any]] = []
        detail_checks = 0
        for item in ordered_candidates:
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
        requested_count = min(validated.max_results, MAX_GMAIL_READ_RESULTS)
        candidate_count = len(results)
        details, failed_ids, beyond_limit = await _fetch_details_bounded(
            service=service,
            user_id=user_id,
            connection_id=connection_id,
            summaries=results,
            limit=requested_count,
        )
        result = {
            "intent": intent,
            "search_criteria": search_criteria,
            "messages": details,
            "gmail_sources": _gmail_sources(
                str(connection_id),
                details,
            ),
            "result_size_estimate": total_estimate,
        }
        return _annotate_retrieval(
            result,
            requested_count=requested_count,
            candidate_count=candidate_count,
            estimates=total_estimate,
            details=details,
            failed_ids=failed_ids,
            beyond_limit=beyond_limit,
            has_more=total_estimate > len(results),
        )

    if intent == "get_latest_email":
        validated = GetLatestEmailPlan.model_validate_json(
            json.dumps(plan, ensure_ascii=False)
        )
        is_sent = validated.folder == "sent"
        if is_sent:
            folder = "sent"
            # The folder mapping supplies the authoritative in:sent constraint.
            query: str | None = None
        elif validated.sender:
            folder = "all"
            query = f"from:{_quote_search_value(validated.sender)}"
        else:
            folder = "all"
            query = "-from:me"
        candidates, total_estimate = await _collect_message_pages(
            service=service,
            user_id=user_id,
            connection_id=connection_id,
            query=query,
            folder=folder,
        )
        candidates = _sort_by_internal_date(candidates)
        # Gmail's folder/query constraint is authoritative; labels are an
        # additional guard against mixed or stale API/cache results.
        guarded = []
        for item in candidates:
            labels = item.get("labels")
            has_sent_label = isinstance(labels, list) and any(
                isinstance(label, str) and label.upper() == "SENT"
                for label in labels
            )
            if is_sent:
                if not labels or has_sent_label:
                    guarded.append(item)
            elif not has_sent_label:
                guarded.append(item)
        candidates = guarded
        logger.info(
            "gmail_latest_lookup | connection_id=%s | folder=%s | "
            "candidate_count=%d | result_estimate=%d",
            connection_id,
            folder,
            len(candidates),
            total_estimate,
        )
        if not candidates:
            return {
                "intent": intent,
                "folder": validated.folder,
                "message": None,
                "gmail_sources": _gmail_sources(str(connection_id), []),
                "result_size_estimate": 0,
            }

        search_plan = (
            SearchEmailsPlan(
                intent="search_emails",
                sender=validated.sender,
                max_results=10,
            )
            if validated.sender
            else None
        )
        requested_count = min(
            validated.max_results,
            MAX_GMAIL_READ_RESULTS,
        )
        details: list[dict[str, Any]] = []
        selected_ids: list[str] = []
        failed_ids: list[str] = []
        for candidate in candidates:
            selected_id = candidate.get("id")
            if not isinstance(selected_id, str) or not selected_id:
                continue
            candidate_detail = await service.get_message(
                user_id=user_id,
                connection_id=connection_id,
                message_id=selected_id,
                include_html=True,
            )
            if search_plan is not None:
                detail_headers = candidate_detail.get("headers") or {}
                verified_sender = {
                    "sender": (
                        detail_headers.get("from")
                        or candidate_detail.get("sender")
                        or candidate.get("sender")
                    ),
                }
                if not _search_metadata_matches(verified_sender, search_plan):
                    failed_ids.append(selected_id)
                    continue
            details.append(_compact_message(candidate_detail))
            selected_ids.append(selected_id)
            if len(details) >= requested_count:
                break
        if not details:
            return {
                "intent": intent,
                "folder": validated.folder,
                "message": None,
                "messages": [],
                "requested_count": requested_count,
                "candidate_count": 0,
                "retrieved_count": 0,
                "processed_count": 0,
                "failed_count": len(failed_ids),
                "remaining_count": 0,
                "retrieved_message_ids": [],
                "failed_message_ids": failed_ids,
                "gmail_sources": _gmail_sources(str(connection_id), []),
                "result_size_estimate": 0,
            }
        logger.info(
            "gmail_latest_selected | connection_id=%s | folder=%s | "
            "message_id=%s | count=%d",
            connection_id,
            folder,
            selected_ids[0],
            len(selected_ids),
        )
        response = {
            "intent": intent,
            "folder": validated.folder,
            "search_criteria": query or f"in:{folder}",
            "message": details[0],
            "gmail_sources": _gmail_sources(
                str(connection_id),
                details,
            ),
            "result_size_estimate": len(selected_ids),
        }
        if requested_count > 1:
            response["messages"] = details
            response.update({
                "requested_count": requested_count,
                "candidate_count": len(selected_ids),
                "retrieved_count": len(details),
                "processed_count": len(details),
                "failed_count": len(failed_ids),
                "remaining_count": 0,
                "retrieved_message_ids": selected_ids,
                "failed_message_ids": failed_ids,
                "has_more": total_estimate > len(details),
            })
        return response

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
        requested_count = min(validated.max_results, MAX_GMAIL_READ_RESULTS)
        unread, total_estimate, has_more = await _collect_message_pages_bounded(
            service=service,
            user_id=user_id,
            connection_id=connection_id,
            query="is:unread",
            folder="inbox",
            page_size=min(PAGE_SIZE, max(10, requested_count)),
            target_count=requested_count,
        )
        candidate_count = len(unread)
        messages, failed_ids, beyond_limit = await _fetch_details_bounded(
            service=service,
            user_id=user_id,
            connection_id=connection_id,
            summaries=unread,
            limit=requested_count,
        )
        result = {
            "intent": intent,
            "messages": messages,
            "gmail_sources": _gmail_sources(
                str(connection_id),
                messages,
            ),
            "result_size_estimate": total_estimate,
        }
        return _annotate_retrieval(
            result,
            requested_count=requested_count,
            candidate_count=candidate_count,
            estimates=total_estimate,
            details=messages,
            failed_ids=failed_ids,
            beyond_limit=beyond_limit,
            has_more=has_more,
        )

    if intent == "summarize_important":
        validated = SummarizeImportantPlan.model_validate_json(
            json.dumps(plan, ensure_ascii=False),
        )
        query, query_details = build_important_search_query(
            validated,
            user_timezone=user_timezone,
            request_time=request_time,
        )
        requested_count = min(validated.max_results, MAX_GMAIL_READ_RESULTS)
        fetched, total_estimate, has_more = await _collect_message_pages_bounded(
            service=service,
            user_id=user_id,
            connection_id=connection_id,
            query=query,
            folder=query_details["folder"],
            page_size=min(PAGE_SIZE, max(10, requested_count)),
            target_count=requested_count,
        )
        start = query_details["start"]
        end = query_details["end"]
        start_epoch = start.timestamp() if start is not None else None
        end_epoch = end.timestamp() if end is not None else None
        summaries: list[dict[str, Any]] = []
        for item in fetched:
            if start_epoch is None and end_epoch is None:
                summaries.append(item)
                continue
            epoch = _internal_epoch(item)
            if epoch is None:
                continue
            if start_epoch is not None and epoch < start_epoch:
                continue
            if end_epoch is not None and epoch >= end_epoch:
                continue
            summaries.append(item)
        candidate_count = len(summaries)
        summaries = _sort_by_internal_date(summaries)[:requested_count]
        messages, failed_ids, beyond_limit = await _fetch_details_bounded(
            service=service,
            user_id=user_id,
            connection_id=connection_id,
            summaries=summaries,
            limit=requested_count,
        )
        result = {
            "intent": intent,
            "period": query_details["period_label"],
            "search_criteria": query,
            "scope": validated.scope,
            "time_zone": query_details["time_zone"],
            "criteria": query_details["criteria"],
            "inbox_prioritized": validated.scope == "inbox",
            "verified_count": candidate_count,
            "messages": messages,
            "gmail_sources": _gmail_sources(
                str(connection_id),
                messages,
            ),
            "result_size_estimate": total_estimate,
        }
        return _annotate_retrieval(
            result,
            requested_count=requested_count,
            candidate_count=candidate_count,
            estimates=total_estimate,
            details=messages,
            failed_ids=failed_ids,
            beyond_limit=beyond_limit,
            has_more=has_more,
        )

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
        requested_count = min(validated.max_results, MAX_GMAIL_READ_RESULTS)
        candidates, total_estimate, has_more = await _collect_message_pages_bounded(
            service=service,
            user_id=user_id,
            connection_id=connection_id,
            query=query,
            folder="all",
            page_size=min(PAGE_SIZE, max(10, requested_count)),
            target_count=requested_count,
        )
        summaries = [
            item
            for item in candidates
            if isinstance(item, dict)
        ]
        candidate_count = len(summaries)
        messages, failed_ids, beyond_limit = await _fetch_details_bounded(
            service=service,
            user_id=user_id,
            connection_id=connection_id,
            summaries=summaries,
            limit=requested_count,
        )
        result = {
            "intent": intent,
            "period": "the last 7 days",
            "search_criteria": query,
            "gmail_important_is_not_ai_urgency": True,
            "messages": messages,
            "gmail_sources": _gmail_sources(
                str(connection_id),
                messages,
            ),
            "result_size_estimate": total_estimate,
        }
        return _annotate_retrieval(
            result,
            requested_count=requested_count,
            candidate_count=candidate_count,
            estimates=total_estimate,
            details=messages,
            failed_ids=failed_ids,
            beyond_limit=beyond_limit,
            has_more=has_more,
        )

    if intent == "summarize_today":
        validated = SummarizeTodayPlan.model_validate_json(
            json.dumps(plan, ensure_ascii=False),
        )
        try:
            zone = ZoneInfo(user_timezone)
        except Exception:
            zone = ZoneInfo("UTC")
            user_timezone = "UTC"
        now = _parse_request_time(request_time) or datetime.now(timezone.utc)
        today = now.astimezone(zone).date()
        start = datetime.combine(today, datetime.min.time(), tzinfo=zone)
        end = start + timedelta(days=1)
        query = (
            f"after:{int(start.timestamp()) - 1} "
            f"before:{int(end.timestamp())} -from:me"
        )
        requested_count = min(validated.max_results, MAX_GMAIL_READ_RESULTS)
        fetched, total_estimate, has_more = await _collect_message_pages_bounded(
            service=service,
            user_id=user_id,
            connection_id=connection_id,
            query=query,
            folder="all",
            page_size=min(PAGE_SIZE, max(10, requested_count)),
            target_count=requested_count,
        )
        # Gmail's query is a filter, not proof. Validate each message's real
        # internalDate against the requested local-day UTC interval.
        start_epoch = start.timestamp()
        end_epoch = end.timestamp()
        summaries: list[dict[str, Any]] = []
        out_of_window_ids: list[str] = []
        for item in fetched:
            item_epoch = _internal_epoch(item)
            if item_epoch is None or not (start_epoch <= item_epoch < end_epoch):
                message_id = item.get("id") or item.get("message_id")
                if isinstance(message_id, str) and message_id:
                    out_of_window_ids.append(message_id)
                continue
            summaries.append(item)
        candidate_count = len(summaries)
        summaries = _sort_by_internal_date(summaries)[:requested_count]
        details, failed_ids, beyond_limit = await _fetch_details_bounded(
            service=service,
            user_id=user_id,
            connection_id=connection_id,
            summaries=summaries,
            limit=requested_count,
        )
        # Gmail's query is a filter, not proof. Messages outside the verified
        # local-day window are excluded from the candidate set, not retrieval
        # failures; counting them as failures corrupts the completeness model.
        logger.info(
            "gmail_today_boundaries | connection_id=%s | timezone=%s | "
            "local_date=%s | start_epoch=%d | end_epoch=%d | "
            "fetched=%d | verified=%d | retrieved=%d",
            connection_id,
            user_timezone,
            today.isoformat(),
            int(start_epoch),
            int(end_epoch),
            len(fetched),
            candidate_count,
            len(details),
        )
        result = {
            "intent": intent,
            "period": f"today ({today.isoformat()}, {user_timezone})",
            "search_criteria": query,
            "local_day_start": start.isoformat(),
            "local_day_end": end.isoformat(),
            "messages": details,
            "gmail_sources": _gmail_sources(
                str(connection_id),
                details,
            ),
            "result_size_estimate": total_estimate,
            "out_of_window_message_ids": out_of_window_ids,
        }
        return _annotate_retrieval(
            result,
            requested_count=requested_count,
            candidate_count=candidate_count,
            estimates=total_estimate,
            details=details,
            failed_ids=failed_ids,
            beyond_limit=beyond_limit,
            has_more=has_more,
        )

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
            user_timezone,
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


async def _fetch_details_bounded(
    *,
    service: GmailService,
    user_id: UUID,
    connection_id: UUID,
    summaries: list[dict[str, Any]],
    limit: int,
) -> tuple[list[dict[str, Any]], list[str], int]:
    """Fetch authoritative details in service-safe chunks of at most 10 IDs."""
    requested_ids: list[str] = []
    seen_ids: set[str] = set()
    for item in summaries:
        if not isinstance(item, dict):
            continue
        message_id = item.get("id") or item.get("message_id")
        if not isinstance(message_id, str) or not message_id:
            continue
        if message_id in seen_ids:
            continue
        seen_ids.add(message_id)
        requested_ids.append(message_id)

    bounded_ids = requested_ids[:limit]
    beyond_limit = max(0, len(requested_ids) - len(bounded_ids))
    details: list[dict[str, Any]] = []
    failed_ids: list[str] = []
    seen_details: set[str] = set()

    for start in range(0, len(bounded_ids), 10):
        chunk = bounded_ids[start:start + 10]
        try:
            chunk_details = await service.get_messages_for_summary(
                user_id=user_id,
                connection_id=connection_id,
                message_ids=chunk,
            )
            by_id: dict[str, dict[str, Any]] = {}
            for detail in chunk_details or []:
                if not isinstance(detail, dict):
                    continue
                detail_id = detail.get("id") or detail.get("message_id")
                if isinstance(detail_id, str) and detail_id in chunk:
                    by_id.setdefault(detail_id, detail)
            for message_id in chunk:
                detail = by_id.get(message_id)
                if detail is None:
                    failed_ids.append(message_id)
                    continue
                if message_id in seen_details:
                    continue
                seen_details.add(message_id)
                details.append(_compact_message(detail))
        except Exception as exc:
            logger.warning(
                "gmail_bounded_detail_chunk_failed | count=%d | error_type=%s",
                len(chunk),
                type(exc).__name__,
            )
            get_message = getattr(service, "get_message", None)
            if get_message is None:
                failed_ids.extend(chunk)
                continue
            for message_id in chunk:
                try:
                    detail = await get_message(
                        user_id=user_id,
                        connection_id=connection_id,
                        message_id=message_id,
                        include_html=False,
                        max_body_chars=MAX_EMAIL_BODY_CHARS,
                    )
                except Exception:
                    failed_ids.append(message_id)
                    continue
                if not isinstance(detail, dict):
                    failed_ids.append(message_id)
                    continue
                normalized = dict(detail)
                normalized.setdefault("id", message_id)
                if message_id in seen_details:
                    continue
                seen_details.add(message_id)
                details.append(_compact_message(normalized))

    return details, failed_ids, beyond_limit


def _annotate_retrieval(
    result: dict[str, Any],
    *,
    requested_count: int,
    candidate_count: int,
    estimates: int,
    details: list[dict[str, Any]],
    failed_ids: list[str],
    beyond_limit: int,
    has_more: bool,
) -> dict[str, Any]:
    """Attach authoritative counts without claiming unverified completeness."""
    retrieved_ids = [
        item.get("message_id")
        for item in details
        if isinstance(item, dict) and item.get("message_id")
    ]
    estimate_gap = max(0, estimates - len(details))
    bounded_gap = max(
        0,
        candidate_count - len(details) - len(failed_ids),
        beyond_limit,
    )
    if result.get("content_truncated"):
        bounded_gap = max(bounded_gap, 1)
    remaining = max(estimate_gap, bounded_gap, 1 if has_more else 0)
    result.update({
        "requested_count": requested_count,
        "candidate_count": candidate_count,
        "retrieved_count": len(details),
        "processed_count": len(details),
        "failed_count": len(failed_ids),
        "remaining_count": remaining,
        "retrieved_message_ids": retrieved_ids,
        "failed_message_ids": failed_ids,
        "has_more": bool(has_more or remaining),
    })
    return result
