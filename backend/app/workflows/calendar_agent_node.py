import asyncio
import json
import logging
import re
from html.parser import HTMLParser
from urllib.parse import urlsplit
from datetime import date, datetime, time, timedelta, timezone, UTC
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from langgraph.runtime import Runtime
from pydantic import ValidationError

from app.db.session import SessionLocal
from app.schemas.calendar_approval import (
    CalendarCreateEventParameters,
    CalendarDeleteEventParameters,
    CalendarUpdateEventParameters,
)
from app.services.calendar_service import CalendarService
from app.services.action_approval_service import ActionApprovalService
from app.llm.streaming import emit_status
from app.workflows.calendar_tools import create_calendar_tools
from app.workflows.chat_state import ChatContext, ChatState
from app.workflows.calendar_create_draft import (
    CalendarCreateDraft,
    draft_missing_fields,
    draft_to_event_arguments,
)
from app.workflows.calendar_update_parser import (
    merge_calendar_update_arguments,
    log_calendar_update_diagnostic,
    parse_calendar_delete_request,
)

logger = logging.getLogger(__name__)
CALENDAR_LIST_MAX_PAGES = 100


async def _list_all_calendars(tool: Any) -> str:
    calendars: list[Any] = []
    page_token: str | None = None
    seen_page_tokens: set[str] = set()
    for _ in range(CALENDAR_LIST_MAX_PAGES):
        arguments = {"page_token": page_token} if page_token else {}
        output = json.loads(await tool.ainvoke(arguments))
        if not isinstance(output, dict):
            return json.dumps({
                "ok": False,
                "error": {
                    "message": "Google Calendar returned an invalid calendar list.",
                },
            })
        if output.get("ok") is not True:
            return json.dumps(output)
        data = output.get("data")
        page_calendars = (
            data.get("calendars") if isinstance(data, dict) else None
        )
        if not isinstance(page_calendars, list):
            return json.dumps({
                "ok": False,
                "error": {
                    "message": "Google Calendar returned an incomplete calendar list.",
                },
            })
        calendars.extend(page_calendars)
        next_page_token = data.get("next_page_token")
        if next_page_token is None:
            return json.dumps({
                "ok": True,
                "data": {"calendars": calendars},
            })
        if (
            not isinstance(next_page_token, str)
            or not next_page_token
            or next_page_token in seen_page_tokens
        ):
            return json.dumps({
                "ok": False,
                "error": {
                    "message": "Google Calendar returned invalid calendar pagination.",
                },
            })
        seen_page_tokens.add(next_page_token)
        page_token = next_page_token
    return json.dumps({
        "ok": False,
        "error": {
            "message": (
                "Google Calendar returned too many calendar pages to "
                "complete this request safely."
            ),
        },
    })


async def _list_all_calendar_events(
    tool: Any,
    base_arguments: dict[str, Any],
) -> str:
    events: list[Any] = []
    page_token: str | None = None
    seen_page_tokens: set[str] = set()
    result_data: dict[str, Any] | None = None
    for _ in range(CALENDAR_LIST_MAX_PAGES):
        arguments = {
            key: value
            for key, value in base_arguments.items()
            if key != "page_token"
        }
        if page_token:
            arguments["page_token"] = page_token
        output = json.loads(await tool.ainvoke(arguments))
        if not isinstance(output, dict):
            return json.dumps({
                "ok": False,
                "error": {
                    "message": "Google Calendar returned an invalid event list.",
                },
            })
        if output.get("ok") is not True:
            return json.dumps(output)
        data = output.get("data")
        page_events = data.get("events") if isinstance(data, dict) else None
        if not isinstance(page_events, list) or not all(
            isinstance(event, dict) for event in page_events
        ):
            return json.dumps({
                "ok": False,
                "error": {
                    "message": "Google Calendar returned an incomplete event list.",
                },
            })
        if result_data is None:
            result_data = dict(data)
        events.extend(page_events)
        next_page_token = data.get("next_page_token")
        if next_page_token is None:
            return json.dumps({
                "ok": True,
                "data": {
                    **result_data,
                    "events": events,
                    "next_page_token": None,
                },
            })
        if (
            not isinstance(next_page_token, str)
            or not next_page_token
            or next_page_token in seen_page_tokens
        ):
            return json.dumps({
                "ok": False,
                "error": {
                    "message": "Google Calendar returned invalid event pagination.",
                },
            })
        seen_page_tokens.add(next_page_token)
        page_token = next_page_token
    return json.dumps({
        "ok": False,
        "error": {
            "message": (
                "Google Calendar returned too many event pages to "
                "complete this request safely."
            ),
        },
    })


def _is_next_event_details_request(message: str) -> bool:
    return bool(re.search(
        r"\b(?:show|get|find|tell me|what is|what's)\b.{0,50}"
        r"\bdetails?\b.{0,40}\b(?:next|upcoming)\s+(?:meeting|event)\b|"
        r"\bdetails?\b.{0,40}\b(?:next|upcoming)\s+(?:meeting|event)\b",
        message.casefold(),
    ))


def _requested_calendar_name(message: str) -> str | None:
    if re.search(
        r"\b(?:my|the)\s+connected\s+Google\s+Calendar\b",
        message,
        re.IGNORECASE,
    ):
        return None
    match = re.search(
        r"\b(?:my|the)\s+([a-z0-9][\w &'’-]{0,39}?)\s+calendar\b",
        message,
        re.IGNORECASE,
    )
    if match is None:
        match = re.search(
            r"\b(?:show|list|view|open)\s+"
            r"([a-z0-9][\w &'’-]{0,39}?)\s+calendar\b",
            message,
            re.IGNORECASE,
        )
    if match is None:
        return None
    name = match.group(1).strip(" .,'\"")
    if not name:
        return None
    words = name.split()
    lowered_words = [word.casefold() for word in words]
    temporal_modifiers = {
        "upcoming", "next", "today", "todays", "tomorrow",
        "yesterday", "yesterdays", "this", "current",
    }
    if name.casefold() in temporal_modifiers or any(
        word in temporal_modifiers for word in lowered_words
    ):
        # Temporal phrases like "upcoming calendar"/"today's calendar" are not
        # explicit calendar-name selections.
        return None
    non_name_modifiers = {"google", "primary", "my", "me", "the", "all"}
    if "all" in lowered_words:
        return None
    if name.casefold() in {"all", "all my", "me all my"}:
        return None
    if all(word in non_name_modifiers for word in lowered_words):
        return None
    if name.casefold() in {
        "google", "primary", "my", "the", "me", "me my",
        "my google", "my primary", "the primary", "my calendar",
    }:
        return None
    return name


def _requested_title_substring(message: str) -> str | None:
    match = re.search(
        r"\btitle(?:s)?\s+contain(?:s)?\s+[\"“](.+?)[\"”]",
        message,
        re.IGNORECASE,
    )
    if match is None:
        return None
    title = match.group(1).strip()
    return title or None


def _matching_calendars(
    calendars: list[Any],
    requested_name: str,
) -> list[dict[str, Any]]:
    valid_calendars = [
        calendar
        for calendar in calendars
        if isinstance(calendar, dict)
        and isinstance(calendar.get("id"), str)
        and calendar["id"].strip()
        and isinstance(calendar.get("summary"), str)
        and calendar["summary"].strip()
    ]
    exact_matches = [
        calendar
        for calendar in valid_calendars
        if calendar["summary"].strip().casefold()
        == requested_name.casefold()
    ]
    if exact_matches:
        return exact_matches
    return [
        calendar
        for calendar in valid_calendars
        if requested_name.casefold()
        in calendar["summary"].strip().casefold()
    ]


def _parse_request_time(value: object) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(
            value[:-1] + "+00:00" if value.endswith("Z") else value
        )
    else:
        raise ValueError("Missing or invalid Calendar request time.")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Calendar request time must include a UTC offset.")
    return parsed


def _event_start_in_timezone(
    event: dict[str, Any],
    timezone_name: str,
) -> datetime | None:
    start = event.get("start")
    if not isinstance(start, dict):
        return None
    raw_datetime = start.get("date_time")
    if isinstance(raw_datetime, str):
        try:
            parsed = datetime.fromisoformat(
                raw_datetime[:-1] + "+00:00"
                if raw_datetime.endswith("Z")
                else raw_datetime
            )
        except ValueError:
            return None
        return (
            parsed
            if parsed.tzinfo is not None and parsed.utcoffset() is not None
            else None
        )
    raw_date = start.get("date")
    if isinstance(raw_date, str):
        try:
            return datetime.combine(
                date.fromisoformat(raw_date),
                time.min,
                tzinfo=ZoneInfo(timezone_name),
            )
        except (ValueError, ZoneInfoNotFoundError):
            return None
    return None


def _select_next_event(
    events: list[Any],
    request_time: datetime,
    timezone_name: str,
) -> dict[str, Any] | None:
    upcoming: list[tuple[datetime, dict[str, Any]]] = []
    for event in events:
        if not isinstance(event, dict):
            return None
        start = _event_start_in_timezone(event, timezone_name)
        if start is None:
            return None
        start_utc = start.astimezone(timezone.utc)
        if start_utc >= request_time.astimezone(timezone.utc):
            upcoming.append((start_utc, event))
    if not upcoming:
        return {}
    return min(upcoming, key=lambda item: item[0])[1]


def _clarification(state: ChatState) -> str:
    lowered = state["user_message"].casefold()
    text = state["user_message"].strip()
    # Generic Calendar requests should default to a useful overview, not force IDs.
    if re.fullmatch(
        r"\s*(?:my\s+)?(?:google\s+)?calendar\s*[.!?]*\s*",
        text,
        re.IGNORECASE,
    ) or re.fullmatch(
        r"\s*(?:please\s+)?(?:check|show|open|view)\s+"
        r"(?:my\s+)?(?:google\s+)?calendar\s*[.!?]*\s*",
        text,
        re.IGNORECASE,
    ) or re.fullmatch(
        r"\s*(?:what(?:'s| is)\s+on\s+)?(?:my\s+)?"
        r"(?:google\s+)?calendar\s*[.!?]*\s*",
        text,
        re.IGNORECASE,
    ):
        return (
            "Let me show your Google Calendar for today in your selected "
            "timezone."
        )
    if re.search(r"\b(?:free|available|availability)\b", lowered):
        if re.search(
            r"\b(?:utc|gmt|pst|pdt|mst|mdt|cst|cdt|est|edt|bst)\b",
            lowered,
        ):
            return (
                "I couldn’t safely interpret that timezone abbreviation. "
                "Please provide an IANA timezone, such as "
                "America/Los_Angeles."
            )
        if not re.search(
            r"\b(?:today|tomorrow|monday|tuesday|wednesday|thursday|"
            r"friday|saturday|sunday|20\d{2}-\d{2}-\d{2})\b",
            lowered,
        ):
            return "Which date should I check?"
        return (
            "What date and time range should I check? Please include the "
            "start and end time; I’ll use your selected timezone."
        )
    if re.search(r"\b(?:overlapping|conflicts?)\b", lowered):
        return (
            "Which date or date range should I check for overlapping "
            "Calendar events?"
        )
    if re.search(r"\b(?:create|add|book|schedule)\b", lowered):
        return (
            "What event would you like to create? Please include its title, "
            "date, and time."
        )
    if re.search(r"\b(?:cancel|delete|remove)\b", lowered):
        return (
            "Which Calendar event would you like to remove? Please include "
            "its title, time, or event ID."
        )
    if re.search(
        r"\b(?:move|reschedule|update|edit|change|modify|invite|rsvp)\b",
        lowered,
    ):
        return (
            "Which Calendar event do you mean, and what would you like to "
            "change?"
        )
    return (
        "I couldn’t determine which Calendar information you want. Please "
        "tell me what to check (for example, today's events, a date range, "
        "or a specific event title)."
    )


def _format_event_time(
    event_time: dict[str, Any] | None,
    timezone: str,
) -> str:
    if isinstance(event_time, datetime):
        # Some call sites may pass a boundary value directly.
        try:
            parsed = event_time
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                return "time unavailable"
            return parsed.astimezone(ZoneInfo(timezone)).strftime("%Y-%m-%d %I:%M %p %Z")
        except Exception:
            return "time unavailable"
    if isinstance(event_time, date) and not isinstance(event_time, datetime):
        return f"{event_time.isoformat()} (all day)"
    if isinstance(event_time, str):
        try:
            parsed = datetime.fromisoformat(
                event_time[:-1] + "+00:00" if event_time.endswith("Z") else event_time
            )
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                # Treat plain YYYY-MM-DD strings as all-day dates.
                raise ValueError("no offset")
            return parsed.astimezone(ZoneInfo(timezone)).strftime("%Y-%m-%d %I:%M %p %Z")
        except Exception:
            try:
                return f"{date.fromisoformat(event_time)} (all day)"
            except Exception:
                return "time unavailable"
    if not isinstance(event_time, dict):
        return "time unavailable"
    all_day = event_time.get("date")
    if isinstance(all_day, str):
        return f"{all_day} (all day)"
    if isinstance(all_day, date) and not isinstance(all_day, datetime):
        return f"{all_day.isoformat()} (all day)"
    raw = event_time.get("date_time")
    if raw is None:
        raw = event_time.get("dateTime")
    if isinstance(raw, datetime):
        try:
            parsed = raw
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                return "time unavailable"
            local = parsed.astimezone(ZoneInfo(timezone))
        except Exception:
            return "time unavailable"
        return local.strftime("%Y-%m-%d %I:%M %p %Z")
    if not isinstance(raw, str):
        return "time unavailable"
    try:
        parsed = datetime.fromisoformat(
            raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        )
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return "time unavailable"
        local = parsed.astimezone(ZoneInfo(timezone))
    except Exception:
        return "time unavailable"
    return local.strftime("%Y-%m-%d %I:%M %p %Z")


def _format_local_datetime(value: Any, timezone: str) -> str:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date) and not isinstance(value, datetime):
        return f"{value.isoformat()} (all day)"
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(
                value[:-1] + "+00:00" if value.endswith("Z") else value
            )
        except ValueError:
            try:
                return f"{date.fromisoformat(value)} (all day)"
            except Exception:
                return "time unavailable"
    else:
        return "time unavailable"
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return "time unavailable"
    try:
        local = parsed.astimezone(ZoneInfo(timezone))
    except Exception:
        return "time unavailable"
    return local.strftime("%Y-%m-%d %I:%M %p %Z")


def _calendar_create_missing_details_prompt(fields: list[str]) -> str:
    prompts = {
        "title": "What title should I use for the event?",
        "date": "What date should I use for the event?",
        "event_date": "What date should I use for the event?",
        "start_time": "What start time should I use?",
        "end_time": "What end time should I use?",
        "time_zone": "What timezone should I use?",
        "calendar": "Which Calendar should I use?",
    }
    questions = list(dict.fromkeys(
        prompts.get(field, "Please correct the invalid event detail.")
        for field in fields
    ))
    if not questions:
        return "Please provide the missing Calendar event details."
    return " ".join(questions)


class _CalendarDescriptionParser(HTMLParser):
    _HIDDEN_TAGS = {"script", "style", "template", "noscript"}
    _BLOCK_TAGS = {
        "address", "article", "aside", "blockquote", "div", "footer",
        "h1", "h2", "h3", "h4", "h5", "h6", "header", "li", "ol",
        "p", "pre", "section", "table", "tr", "ul",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[str] = []
        self.hidden_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag in self._HIDDEN_TAGS:
            self.hidden_depth += 1
            return
        if self.hidden_depth:
            return
        if tag == "br":
            self.parts.append("\n")
        elif tag in self._BLOCK_TAGS:
            self._append_break()
        elif tag == "li":
            self.parts.append("• ")
        if tag == "a":
            href = dict(attrs).get("href")
            if isinstance(href, str):
                link = href.strip()
                if self._safe_link(link) and link not in self.links:
                    self.links.append(link)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in self._HIDDEN_TAGS:
            if self.hidden_depth:
                self.hidden_depth -= 1
            return
        if self.hidden_depth:
            return
        if tag in self._BLOCK_TAGS:
            self._append_break()

    def handle_data(self, data: str) -> None:
        if not self.hidden_depth:
            self.parts.append(data)

    def _append_break(self) -> None:
        text = "".join(self.parts)
        if text and not text.endswith("\n"):
            self.parts.append("\n")

    def text(self) -> str:
        text = "".join(self.parts)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n[ \t]+", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @staticmethod
    def _safe_link(value: str) -> bool:
        try:
            parsed = urlsplit(value)
        except ValueError:
            return False
        return parsed.scheme.casefold() in {"http", "https", "mailto"} and (
            parsed.scheme.casefold() == "mailto"
            or bool(parsed.hostname)
        )


def _calendar_description(description: object) -> tuple[str, list[str]]:
    if not isinstance(description, str) or not description.strip():
        return "", []
    if not re.search(r"</?[a-z][^>]*>", description, re.IGNORECASE):
        return description.strip(), []
    parser = _CalendarDescriptionParser()
    try:
        parser.feed(description)
        parser.close()
    except Exception:
        return re.sub(r"<[^>]+>", "", description).strip(), []
    return parser.text(), parser.links


def _safe_meeting_links(event: dict[str, Any], description_links: list[str]) -> list[str]:
    candidates = description_links[:]
    html_link = event.get("html_link")
    if isinstance(html_link, str):
        candidates.append(html_link)
    links: list[str] = []
    for value in candidates:
        if not isinstance(value, str):
            continue
        value = value.strip()
        if value in links:
            continue
        if _CalendarDescriptionParser._safe_link(value):
            links.append(value)
    return links[:10]


def _structured_calendar_events(
    data: dict[str, Any],
    timezone_name: str,
    calendar_name: str | None = None,
) -> list[dict[str, Any]]:
    raw_events = data.get("events")
    if not isinstance(raw_events, list):
        raw_events = [data] if isinstance(data.get("id"), str) else []
    if data.get("calendar_id"):
        calendar_name = calendar_name or str(data["calendar_id"])

    cards: list[dict[str, Any]] = []
    for event in raw_events:
        if not isinstance(event, dict):
            continue
        event_id = event.get("id")
        if not isinstance(event_id, str) or not event_id.strip():
            continue
        description, description_links = _calendar_description(
            event.get("description"),
        )
        attendees = [
            {
                "display_name": item.get("display_name"),
                "email": item.get("email"),
            }
            for item in (event.get("attendees") or [])
            if isinstance(item, dict)
        ]
        cards.append({
            "id": event_id,
            "title": (
                event["summary"].strip()
                if isinstance(event.get("summary"), str)
                and event["summary"].strip()
                else "Untitled event"
            ),
            "date": _event_card_date(event.get("start"), timezone_name),
            "start": _format_event_time(event.get("start"), timezone_name),
            "end": _format_event_time(event.get("end"), timezone_name),
            "timezone": timezone_name,
            "location": (
                event["location"].strip()
                if isinstance(event.get("location"), str)
                and event["location"].strip()
                else None
            ),
            "calendar_name": calendar_name,
            "attendees": attendees,
            "description": description,
            "meeting_links": _safe_meeting_links(event, description_links),
        })
    return cards


def _event_card_date(
    event_time: dict[str, Any] | None,
    timezone_name: str,
) -> str | None:
    if not isinstance(event_time, dict):
        return None
    all_day = event_time.get("date")
    if isinstance(all_day, str):
        return all_day
    raw = event_time.get("date_time")
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(
            raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        )
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(ZoneInfo(timezone_name)).strftime("%Y-%m-%d")
    except (ValueError, KeyError):
        return None


def _format_event(
    event: dict[str, Any],
    timezone: str,
    calendar_id: str | None = None,
) -> str:
    summary = event.get("summary")
    title = summary.strip() if isinstance(summary, str) and summary.strip() else "Untitled event"
    start = _format_event_time(event.get("start"), timezone)
    end = _format_event_time(event.get("end"), timezone)
    lines = [f"- **{title}** — {start} to {end}"]
    event_id = event.get("id")
    if isinstance(event_id, str) and event_id.strip():
        lines.append(f"  - Event ID: {event_id}")
    if isinstance(calendar_id, str) and calendar_id.strip():
        lines.append(f"  - Calendar ID: {calendar_id}")
    recurrence = event.get("recurrence")
    recurring_event_id = event.get("recurring_event_id")
    if isinstance(recurrence, list) and all(
        isinstance(rule, str) for rule in recurrence
    ):
        lines.append(f"  - Recurrence: {'; '.join(recurrence)}")
    elif isinstance(recurring_event_id, str) and recurring_event_id.strip():
        lines.append(
            "  - Recurrence: recurring instance; "
            f"master event ID {recurring_event_id}; rule not included"
        )
    else:
        lines.append("  - Recurrence: single event")
    location = event.get("location")
    if isinstance(location, str) and location.strip():
        lines.append(f"  - Location: {location.strip()}")
    description = event.get("description")
    if isinstance(description, str) and description.strip():
        description_text, description_links = _calendar_description(description)
        if description_text:
            lines.append(f"  - Details: {description_text[:2000]}")
        for link in _safe_meeting_links(event, description_links)[:3]:
            lines.append(f"  - Meeting link: {link}")
    attendees = event.get("attendees")
    if isinstance(attendees, list):
        attendee_names = [
            item.get("display_name") or item.get("email")
            for item in attendees
            if isinstance(item, dict)
            and isinstance(item.get("display_name") or item.get("email"), str)
        ]
        if attendee_names:
            lines.append(f"  - Attendees: {', '.join(attendee_names[:10])}")
    return "\n".join(lines)


def _render_result(
    tool_name: str,
    data: dict[str, Any],
    timezone: str,
    requested_calendar_ids: list[str] | None = None,
) -> str:
    if tool_name == "list_calendars":
        calendars = data.get("calendars")
        if not isinstance(calendars, list) or any(
            not isinstance(calendar, dict)
            or not isinstance(calendar.get("id"), str)
            or not isinstance(calendar.get("summary"), str)
            for calendar in calendars
        ):
            return (
                "Google Calendar returned malformed calendar data, so I "
                "couldn’t provide a reliable list."
            )
        if not calendars:
            return "No calendars are accessible to the selected Google account."
        lines = []
        for calendar in calendars:
            summary = calendar["summary"].strip() or "Untitled calendar"
            details = []
            if calendar.get("primary") is True:
                details.append("primary")
            role = calendar.get("access_role")
            if isinstance(role, str) and role.strip():
                details.append(f"access: {role.strip()}")
            timezone_name = calendar.get("time_zone")
            if isinstance(timezone_name, str) and timezone_name.strip():
                details.append(f"timezone: {timezone_name.strip()}")
            suffix = f" ({', '.join(details)})" if details else ""
            lines.append(f"- **{summary}**{suffix}")
        return "Accessible Google Calendars:\n\n" + "\n".join(lines)

    if tool_name == "check_calendar_availability":
        calendars = data.get("calendars")
        if not isinstance(calendars, dict) or not calendars:
            return (
                "I couldn’t determine your availability because Google "
                "Calendar returned no complete availability data."
            )
        if requested_calendar_ids and not set(requested_calendar_ids).issubset(
            calendars
        ):
            return (
                "I couldn’t reliably determine your availability because "
                "Google Calendar returned incomplete results. No availability "
                "claim can be made."
            )
        errors = [
            error
            for calendar in calendars.values()
            if isinstance(calendar, dict)
            for error in (calendar.get("errors") or [])
        ]
        if errors:
            return (
                "I couldn’t reliably determine your availability because "
                "Google Calendar returned incomplete results. No availability "
                "claim can be made."
            )
        busy_intervals = [
            interval
            for calendar in calendars.values()
            if isinstance(calendar, dict)
            for interval in (calendar.get("busy") or [])
            if isinstance(interval, dict)
        ]
        if not busy_intervals:
            scope = (
                "Your primary Google Calendar appears free"
                if requested_calendar_ids == ["primary"]
                else "The selected Google Calendars appear free"
            )
            local_start = _format_local_datetime(
                data.get("time_min"),
                data.get("time_zone", timezone),
            )
            local_end = _format_local_datetime(
                data.get("time_max"),
                data.get("time_zone", timezone),
            )
            return (
                f"{scope} for the requested interval "
                f"({local_start} to {local_end}, "
                f"{data.get('time_zone', timezone)})."
            )
        interval_text = ", ".join(
            f"{_format_event_time({'date_time': interval.get('start')}, timezone)}"
            f"–{_format_event_time({'date_time': interval.get('end')}, timezone)}"
            for interval in busy_intervals[:10]
        )
        return (
            "The selected Google Calendar(s) show busy time during the "
            f"requested interval: {interval_text}."
        )

    events = data.get("events")
    if tool_name == "get_calendar_event":
        if not isinstance(data.get("id"), str) or not data["id"].strip():
            return (
                "Google Calendar returned malformed event details, so I "
                "couldn’t verify this event."
            )
        events = [data]
    elif (
        not isinstance(events, list)
        or any(
            not isinstance(event, dict)
            or not isinstance(event.get("id"), str)
            or not event["id"].strip()
            for event in events
        )
    ):
        return (
            "Google Calendar returned malformed event data, so I couldn’t "
            "provide a reliable answer."
        )
    if not events:
        return "Google Calendar returned no matching events for this live request."
    result = f"Live results from Google Calendar ({len(events)} event(s)):\n\n"
    result += "\n".join(
        _format_event(
            event,
            timezone,
            data.get("calendar_id")
            if isinstance(data.get("calendar_id"), str)
            else None,
        )
        for event in events
        if isinstance(event, dict)
    )

    conflicts = data.get("conflicts")
    if isinstance(conflicts, list) and conflicts:
        event_titles = {
            event.get("id"): event.get("summary") or "Untitled event"
            for event in events
            if isinstance(event, dict)
        }
        conflict_lines = []
        for conflict in conflicts[:10]:
            if not isinstance(conflict, dict):
                continue
            first = event_titles.get(conflict.get("event_id"), "an event")
            second = event_titles.get(
                conflict.get("conflicting_event_id"),
                "another event",
            )
            start = _format_event_time(
                {"date_time": conflict.get("overlap_start")},
                timezone,
            )
            end = _format_event_time(
                {"date_time": conflict.get("overlap_end")},
                timezone,
            )
            conflict_lines.append(f"- {first} overlaps {second} ({start}–{end}).")
        if conflict_lines:
            result += "\n\nOverlaps:\n" + "\n".join(conflict_lines)
    if data.get("next_page_token"):
        result += (
            "\n\nThis is a partial result; more events are available on "
            "the next page."
        )
    if data.get("conflicts_complete") is False:
        result += (
            "\n\nThe event list was incomplete, so overlap detection may "
            "not include every event."
        )
    return result


def _event_snapshot(event: dict[str, Any]) -> dict[str, Any]:
    def snapshot_time(value: object) -> dict[str, str] | None:
        if not isinstance(value, dict):
            return None
        event_date = value.get("date")
        if isinstance(event_date, date):
            return {"date": event_date.isoformat()}
        event_time = value.get("date_time")
        if not isinstance(event_time, datetime):
            return None
        zone = value.get("time_zone")
        if isinstance(zone, str):
            result = {
                "dateTime": event_time.astimezone(ZoneInfo(zone)).isoformat()
            }
            result["timeZone"] = zone
            return result
        return {"dateTime": event_time.isoformat()}

    attendees = event.get("attendees") or []
    snapshot = {
        "summary": event.get("summary"),
        "description": event.get("description"),
        "location": event.get("location"),
        "start": snapshot_time(event.get("start")),
        "end": snapshot_time(event.get("end")),
        "attendees": sorted(
            item["email"].casefold()
            for item in attendees
            if isinstance(item, dict)
            and isinstance(item.get("email"), str)
        ),
        "etag": event.get("etag"),
    }
    recurrence = event.get("recurrence")
    if isinstance(recurrence, list):
        snapshot["recurrence"] = recurrence
    recurring_event_id = event.get("recurring_event_id")
    if isinstance(recurring_event_id, str) and recurring_event_id.strip():
        snapshot["recurring_event_id"] = recurring_event_id
    original_start_time = snapshot_time(event.get("original_start_time"))
    if original_start_time is not None:
        snapshot["original_start_time"] = original_start_time
    return snapshot


def _calendar_event_interval(
    event: dict[str, Any],
    timezone: str,
) -> tuple[datetime, datetime] | None:
    """Return absolute start/end instants for a Calendar event payload."""

    try:
        zone = ZoneInfo(timezone)
    except (ValueError, ZoneInfoNotFoundError):
        zone = ZoneInfo("UTC")

    def parse(value: object) -> datetime | None:
        if not isinstance(value, dict):
            return None
        date_value = value.get("date")
        if isinstance(date_value, date) and not isinstance(
            date_value,
            datetime,
        ):
            return datetime.combine(date_value, time.min, tzinfo=zone)
        if isinstance(date_value, str):
            try:
                return datetime.combine(
                    date.fromisoformat(date_value),
                    time.min,
                    tzinfo=zone,
                )
            except ValueError:
                return None
        date_time = value.get("date_time")
        if isinstance(date_time, datetime):
            parsed = date_time
        elif isinstance(date_time, str):
            try:
                parsed = datetime.fromisoformat(
                    date_time[:-1] + "+00:00"
                    if date_time.endswith("Z")
                    else date_time
                )
            except ValueError:
                return None
        else:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed

    start = parse(event.get("start"))
    end = parse(event.get("end"))
    if start is None or end is None or end <= start:
        return None
    return start, end


def _calendar_conflict_lines(
    events: list[Any],
    start: datetime,
    end: datetime,
    timezone: str,
) -> list[str]:
    conflicts: list[str] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        interval = _calendar_event_interval(event, timezone)
        if interval is None:
            continue
        event_start, event_end = interval
        if event_start >= end or event_end <= start:
            continue
        summary = str(event.get("summary") or "Untitled event").strip()
        conflicts.append(
            f"{summary} ("
            f"{_format_local_datetime(event_start, timezone)} to "
            f"{_format_local_datetime(event_end, timezone)})"
        )
    return conflicts


def _recent_created_calendar_event(
    state: ChatState,
) -> tuple[dict[str, Any] | None, str | None]:
    """Return the most recent trusted Calendar create result in history."""

    user_id = state.get("user_id")
    conversation_id = state.get("conversation_id")
    for message in reversed(state.get("messages", [])):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        metadata = message.get("metadata")
        proposal = (
            metadata.get("action_approval")
            if isinstance(metadata, dict)
            else None
        )
        if not isinstance(proposal, dict):
            continue
        if proposal.get("action_type") != "calendar.create_event":
            continue
        if str(proposal.get("user_id")) != str(user_id):
            continue
        if str(proposal.get("conversation_id")) != str(conversation_id):
            continue
        if proposal.get("status") != "completed":
            continue
        result = proposal.get("result")
        event_id = None
        if isinstance(result, dict):
            candidate = result.get("google_event_id")
            if isinstance(candidate, str) and candidate.strip():
                event_id = candidate.strip()
        if event_id is None:
            candidate = proposal.get("calendar_event_id")
            if isinstance(candidate, str) and candidate.strip():
                event_id = candidate.strip()
        if event_id is None:
            continue

        # Contextual references are intentionally short-lived.
        completed_at_raw = proposal.get("completed_at") or proposal.get(
            "approved_at"
        )
        if not isinstance(completed_at_raw, str) or not completed_at_raw.strip():
            continue
        try:
            completed_at = datetime.fromisoformat(
                completed_at_raw.replace("Z", "+00:00")
            )
        except (TypeError, ValueError):
            continue
        if completed_at.tzinfo is None or completed_at.utcoffset() is None:
            continue
        now_value = state.get("request_time")
        if isinstance(now_value, datetime):
            now = now_value
        elif isinstance(now_value, str):
            try:
                now = datetime.fromisoformat(now_value.replace("Z", "+00:00"))
            except ValueError:
                now = datetime.now(timezone.utc)
        else:
            now = datetime.now(timezone.utc)
        if now.tzinfo is None or now.utcoffset() is None:
            now = now.replace(tzinfo=timezone.utc)
        if completed_at > now + timedelta(minutes=1):
            continue
        if (now - completed_at).total_seconds() > 5 * 60:
            continue
        return proposal, event_id
    return None, None


def _resolve_relative_time_changes(
    event: dict[str, Any],
    changes: dict[str, Any],
    fallback_timezone: str,
) -> tuple[dict[str, Any], str | None]:
    requested_start_time = changes.pop("start_time", None)
    requested_start_date = changes.pop("start_date", None)
    duration_delta_minutes = changes.pop("duration_delta_minutes", None)
    if (
        requested_start_time is None
        and requested_start_date is None
        and duration_delta_minutes is None
    ):
        return changes, None

    start = event.get("start")
    end = event.get("end")
    if (
        not isinstance(start, dict)
        or not isinstance(end, dict)
        or not isinstance(start.get("date_time"), datetime)
        or not isinstance(end.get("date_time"), datetime)
    ):
        raise ValueError("Relative time changes require a timed event.")

    timezone_name = start.get("time_zone") or fallback_timezone
    try:
        zone = ZoneInfo(timezone_name)
    except (TypeError, ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("The event timezone is invalid.") from exc

    event_start = start["date_time"]
    event_end = end["date_time"]
    if (
        event_start.tzinfo is None
        or event_start.utcoffset() is None
        or event_end.tzinfo is None
        or event_end.utcoffset() is None
        or event_end.astimezone(timezone.utc)
        <= event_start.astimezone(timezone.utc)
    ):
        raise ValueError("The event time range is invalid.")

    resolved_start = event_start.astimezone(zone)
    local_start_date = resolved_start.date()
    if requested_start_date is not None:
        if not isinstance(requested_start_date, str):
            raise ValueError("A local ISO start date is required.")
        try:
            local_start_date = date.fromisoformat(requested_start_date)
        except ValueError as exc:
            raise ValueError("A local ISO start date is required.") from exc
    if requested_start_time is not None:
        if not isinstance(requested_start_time, str) or not re.fullmatch(
            r"(?:[01]\d|2[0-3]):[0-5]\d",
            requested_start_time,
        ):
            raise ValueError("A local 24-hour HH:MM start time is required.")
        hour, minute = (int(value) for value in requested_start_time.split(":"))
        naive_start = datetime.combine(
            local_start_date,
            time(hour, minute),
        )
        first = naive_start.replace(tzinfo=zone, fold=0)
        second = naive_start.replace(tzinfo=zone, fold=1)
        if first.utcoffset() != second.utcoffset():
            raise ValueError("The requested start time is ambiguous.")
        if first.astimezone(timezone.utc).astimezone(zone).replace(
            tzinfo=None
        ) != naive_start:
            raise ValueError("The requested start time does not exist.")
        resolved_start = first
    elif requested_start_date is not None:
        local_current_start = event_start.astimezone(zone)
        naive_start = datetime.combine(local_start_date, local_current_start.time())
        first = naive_start.replace(tzinfo=zone, fold=0)
        second = naive_start.replace(tzinfo=zone, fold=1)
        if first.utcoffset() != second.utcoffset():
            raise ValueError("The requested start time is ambiguous.")
        if first.astimezone(timezone.utc).astimezone(zone).replace(
            tzinfo=None
        ) != naive_start:
            raise ValueError("The requested start time does not exist.")
        resolved_start = first

    original_duration = (
        event_end.astimezone(timezone.utc)
        - event_start.astimezone(timezone.utc)
    )
    resolved_end = (
        resolved_start.astimezone(timezone.utc) + original_duration
    ).astimezone(zone)

    if duration_delta_minutes is not None:
        if (
            isinstance(duration_delta_minutes, bool)
            or not isinstance(duration_delta_minutes, int)
            or not -1440 <= duration_delta_minutes <= 1440
        ):
            raise ValueError("The duration change is outside the allowed range.")
        resolved_end = (
            resolved_end.astimezone(timezone.utc)
            + timedelta(minutes=duration_delta_minutes)
        ).astimezone(zone)
        if resolved_end.astimezone(timezone.utc) <= resolved_start.astimezone(
            timezone.utc
        ):
            raise ValueError("The resulting event duration must be positive.")

    changes["start"] = {
        "dateTime": resolved_start.isoformat(timespec="seconds"),
        "timeZone": zone.key,
    }
    changes["end"] = {
        "dateTime": resolved_end.isoformat(timespec="seconds"),
        "timeZone": zone.key,
    }
    return changes, zone.key


def _is_calendar_update_request(text: str) -> bool:
    first_mutation = re.search(
        r"\b(?:delete|remove|cancel|move|reschedule|update|edit|change|modify)\b",
        text,
        re.IGNORECASE,
    )
    if first_mutation and first_mutation.group(0).casefold() in {
        "delete",
        "remove",
        "cancel",
    }:
        return False
    return bool(re.search(
        r"\b(?:move|reschedule|update|edit|change|modify)\b.{0,100}"
        r"\b(?:my\s+)?(?:existing\s+)?(?:google\s+)?"
        r"(?:calendar\s+)?(?:meetings?|events?|appointments?|sessions?|classes?|calendar)\b|"
        r"\b(?:my\s+)?(?:calendar\s+)?"
        r"(?:meetings?|events?|appointments?|sessions?|classes?)\b.{0,100}"
        r"\b(?:move|reschedule|update|edit|change|modify)\b",
        text,
        re.IGNORECASE,
    ))


def _is_calendar_delete_request(text: str) -> bool:
    first_mutation = re.search(
        r"\b(?:delete|remove|cancel|move|reschedule|update|edit|change|modify)\b",
        text,
        re.IGNORECASE,
    )
    if not first_mutation or first_mutation.group(0).casefold() not in {
        "delete",
        "remove",
        "cancel",
    }:
        return False
    return bool(
        re.search(
            r"\b(?:delete|remove|cancel)\b.{0,120}"
            r"\b(?:calendar\s+)?(?:event|series|meeting|appointment|calendar)\b|"
            r"\b(?:calendar\s+)?(?:event|series|meeting|appointment)\b"
            r".{0,120}\b(?:delete|remove|cancel)\b",
            text,
            re.IGNORECASE,
        )
        or (
            re.search(r"\b(?:delete|remove|cancel)\b", text, re.IGNORECASE)
            and re.search(
                r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b"
                r"\s*(?:to|[-–—])\s*"
                r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b",
                text,
                re.IGNORECASE,
            )
        )
    )


def _update_event_matches_request(
    event: dict[str, Any],
    event_query: str | None,
    details: dict[str, Any],
) -> bool:
    if details.get("event_title_is_prefix"):
        summary = event.get("summary")
        if not isinstance(summary, str) or not isinstance(event_query, str):
            return False
        if not " ".join(summary.casefold().split()).startswith(
            " ".join(event_query.casefold().split())
        ):
            return False
    elif details.get("event_title_is_explicit"):
        summary = event.get("summary")
        if not isinstance(summary, str) or not isinstance(event_query, str):
            return False
        if " ".join(summary.casefold().split()) != " ".join(
            event_query.casefold().split()
        ):
            return False

    event_date = details.get("event_date")
    original_start = details.get("original_start_time")
    original_end = details.get("original_end_time")
    if event_date is None and original_start is None and original_end is None:
        return True

    start = event.get("start")
    end = event.get("end")
    if not isinstance(start, dict) or not isinstance(end, dict):
        return False
    event_start = start.get("date_time")
    event_end = end.get("date_time")
    zone_name = details.get("time_zone")
    if not isinstance(zone_name, str):
        return False
    try:
        zone = ZoneInfo(zone_name)
    except (ValueError, ZoneInfoNotFoundError):
        return False

    if event_date is not None:
        all_day_date = start.get("date")
        if isinstance(all_day_date, date):
            return (
                original_start is None
                and original_end is None
                and all_day_date == event_date
            )
        if not isinstance(event_start, datetime):
            return False
        if event_start.astimezone(zone).date() != event_date:
            return False

    if original_start is not None:
        if not isinstance(event_start, datetime):
            return False
        local_start = event_start.astimezone(zone)
        if (local_start.hour, local_start.minute) != (
            original_start.hour,
            original_start.minute,
        ):
            return False

    if original_end is not None:
        if not isinstance(event_end, datetime):
            return False
        local_end = event_end.astimezone(zone)
        if (local_end.hour, local_end.minute) != (
            original_end.hour,
            original_end.minute,
        ):
            return False
        if event_date is not None:
            expected_end_date = event_date
            if original_start is not None and original_end <= original_start:
                expected_end_date += timedelta(days=1)
            if local_end.date() != expected_end_date:
                return False
    return True


def _matches_recurring_master(
    master: dict[str, Any],
    occurrence: dict[str, Any],
    event_query: str | None,
    details: dict[str, Any],
) -> bool:
    if master.get("recurring_event_id"):
        return False
    is_listed_master = (
        occurrence.get("id") == master.get("id")
        and bool(occurrence.get("recurrence"))
        and not occurrence.get("recurring_event_id")
    )
    if (
        not is_listed_master
        and occurrence.get("recurring_event_id") != master.get("id")
    ):
        return False
    recurrence = master.get("recurrence")
    expected_recurrence = details.get("expected_recurrence")
    if not isinstance(recurrence, list) or not recurrence:
        return False
    expected_frequency = None
    if isinstance(expected_recurrence, list):
        expected_frequency_match = re.search(
            r"(?:^|;)FREQ=([A-Z]+)(?:;|$)",
            expected_recurrence[0].removeprefix("RRULE:").upper(),
        )
        if expected_frequency_match:
            expected_frequency = expected_frequency_match.group(1)
    recurrence_rules = [
        rule.removeprefix("RRULE:").upper()
        for rule in recurrence
        if isinstance(rule, str) and rule.upper().startswith("RRULE:")
    ]
    if expected_frequency is not None and not any(
        re.search(
            rf"(?:^|;)FREQ={re.escape(expected_frequency)}(?:;|$)",
            rule,
        )
        for rule in recurrence_rules
    ):
        return False
    if details.get("expected_recurring_unbounded"):
        has_unbounded_daily_rule = any(
            re.search(r"(?:^|;)FREQ=DAILY(?:;|$)", rule)
            for rule in recurrence_rules
        )
        has_any_recurrence_end = any(
            re.search(r"(?:^|;)(?:UNTIL|COUNT)=", rule)
            for rule in recurrence_rules
        )
        if not has_unbounded_daily_rule or has_any_recurrence_end:
            return False
    occurrence_summary = occurrence.get("summary")
    if (
        not isinstance(occurrence_summary, str)
        or master.get("summary") != occurrence_summary
    ):
        return False

    return _update_event_matches_request(
        master,
        event_query,
        details,
    )


def _prepare_event_mutation(
    state: ChatState,
    runtime: Runtime[ChatContext],
    arguments: object,
    action_type: str,
    update_details: dict[str, Any] | None = None,
    delete_details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    capabilities = (
        (state.get("assistant_capabilities") or {}).get("google_calendar", {})
    )
    operation = "update" if action_type == "calendar.update_event" else "delete"
    enabled_key = f"{operation}_execution_available"
    context = runtime.context
    if not context.get("google_connection_available", False):
        response = (
            "Google Calendar is not connected. Connect a Google account in "
            "Connected Apps. Nothing was changed."
        )
        return {
            "assistant_response": response,
            "provider": "",
            "model": "",
            "messages": [
                {"role": "user", "content": state["user_message"]},
                {"role": "assistant", "content": response},
            ],
        }
    user_id = context.get("user_id")
    connection_id = (
        context.get("calendar_write_connection_id")
        or context.get("calendar_event_connection_id")
    )
    write_permission_granted = capabilities.get(
        "write_permission_granted",
        capabilities.get(enabled_key, False),
    )
    if context.get("calendar_default_connection_invalid", False):
        response = (
            "Your saved default Google Calendar account is unavailable. "
            "Choose a connected account in Connected Apps; nothing was changed."
        )
    elif context.get("calendar_write_selection_required", False):
        response = (
            "More than one Google account can change Calendar events. Choose "
            "your default account in Connected Apps; nothing was changed."
        )
    elif context.get("calendar_selected_write_scope_missing", False):
        response = (
            "Your selected Google account is missing Calendar write permission. "
            "Grant access for that account in Connected Apps; nothing was changed."
        )
    elif not write_permission_granted:
        response = (
            "The selected Google account is missing Calendar write permission. "
            "Grant access for that account in Connected Apps; nothing was changed."
        )
    elif not connection_id or not user_id:
        response = (
            "I couldn’t safely select an authorized Google Calendar account. "
            "Nothing was changed."
        )
    elif not isinstance(arguments, dict):
        response = (
            "Please specify which Calendar event to update and what you "
            "would like to change. Nothing was changed."
            if action_type == "calendar.update_event"
            else "I couldn’t identify the Calendar event. Please specify it."
        )
    else:
        response = ""

    if response:
        return {
            "assistant_response": response,
            "provider": "",
            "model": "",
            "messages": [
                {"role": "user", "content": state["user_message"]},
                {"role": "assistant", "content": response},
            ],
        }
    if action_type == "calendar.update_event" and update_details:
        parse_errors = update_details.get("errors") or []
        if parse_errors:
            response = (
                "I couldn’t safely interpret the requested Calendar "
                f"date or time ({parse_errors[0]}). Please clarify it. "
                "Nothing was changed."
            )
            return {
                "assistant_response": response,
                "provider": "",
                "model": "",
                "messages": [
                    {"role": "user", "content": state["user_message"]},
                    {"role": "assistant", "content": response},
                ],
            }
    if (
        action_type == "calendar.delete_event"
        and delete_details
        and delete_details.get("delete_entire_series")
    ):
        required_fields = (
            delete_details.get("event_query"),
            delete_details.get("event_date"),
            delete_details.get("original_start_time"),
            delete_details.get("original_end_time"),
            delete_details.get("time_zone"),
            delete_details.get("expected_recurrence"),
            delete_details.get("expected_recurring_unbounded"),
        )
        if (
            delete_details.get("errors")
            or any(value is None for value in required_fields)
            or not delete_details.get("expected_recurring_unbounded")
        ):
            response = (
                "I couldn’t safely verify the recurring series. Please "
                "specify its exact title, date, time range, timezone, and "
                "recurrence. Nothing was changed."
            )
            return {
                "assistant_response": response,
                "provider": "",
                "model": "",
                "messages": [
                    {"role": "user", "content": state["user_message"]},
                    {"role": "assistant", "content": response},
                ],
            }

    calendar_name = arguments.get("calendar_name")
    calendar_id = arguments.get("calendar_id")
    if not calendar_id and not calendar_name:
        calendar_id = "primary"
    event_id = arguments.get("event_id")
    event_query = arguments.get("event_query")
    changes = arguments.get("changes")
    lookup_details = (
        update_details
        if action_type == "calendar.update_event"
        else delete_details
    )
    if (
        action_type == "calendar.delete_event"
        and delete_details
        and delete_details.get("delete_entire_series")
    ):
        event_id = None
    trusted_event_ids = state.get("trusted_calendar_event_ids")
    if not isinstance(trusted_event_ids, set):
        trusted_event_ids = set(trusted_event_ids) if isinstance(trusted_event_ids, (list, tuple)) else set()
    latest_message = state["user_message"].casefold()
    if isinstance(event_id, str) and event_id not in trusted_event_ids and event_id.casefold() not in latest_message:
        event_id = None
    if isinstance(event_query, str):
        normalized_query = " ".join(event_query.casefold().split())
        recent_messages = state.get("messages", [])[-6:]
        pending_details = state.get("pending_calendar_action")
        pending_known_details = (
            pending_details.get("known_event_details")
            if isinstance(pending_details, dict)
            else None
        )
        query_from_pending_action = (
            isinstance(pending_known_details, dict)
            and pending_known_details.get("event_query") == event_query
        )
        is_contextual_followup = bool(re.match(
            r"^(?:move|reschedule|change|update|delete|cancel|remove|"
            r"make)\s+(?:it|that|this)\b",
            latest_message.strip(),
        ))
        query_in_recent_context = any(
            isinstance(message, dict)
            and isinstance(message.get("content"), str)
            and normalized_query
            in " ".join(message["content"].casefold().split())
            for message in recent_messages
        )
        if (
            normalized_query not in " ".join(latest_message.split())
            and not (is_contextual_followup and query_in_recent_context)
            and not query_from_pending_action
        ):
            event_query = None
    if action_type == "calendar.update_event":
        missing_event = event_id is None and not (
            isinstance(event_query, str) and event_query.strip()
        )
        missing_changes = not isinstance(changes, dict) or not changes
        if missing_event or missing_changes:
            if missing_event and missing_changes:
                response = (
                    "Please specify which Calendar event to update and what "
                    "you would like to change. Nothing was changed."
                )
            elif missing_event:
                response = (
                    "Which Calendar event should I update? Please include "
                    "its title, time, or event ID. Nothing was changed."
                )
            else:
                details_missing = (
                    update_details.get("missing_fields", [])
                    if update_details
                    else []
                )
                requested_times = [
                    field for field in details_missing
                    if field in {"proposed start time", "proposed end time"}
                ]
                if requested_times:
                    response = (
                        "Please specify the "
                        + " and ".join(requested_times)
                        + " for the Calendar Update. Nothing was changed."
                    )
                elif "timezone" in details_missing:
                    response = (
                        "Please specify the IANA timezone for the proposed "
                        "Calendar times. Nothing was changed."
                    )
                else:
                    response = (
                        "What would you like to change about the Calendar "
                        "event? Nothing was changed."
                    )
            return {
                "assistant_response": response,
                "provider": "",
                "model": "",
                "messages": [
                    {"role": "user", "content": state["user_message"]},
                    {"role": "assistant", "content": response},
                ],
            }
    elif event_id is None and event_query is None and not (
        action_type == "calendar.delete_event"
        and delete_details
        and delete_details.get("generic_event_discovery")
        and delete_details.get("event_date") is not None
    ):
        response = (
            "Which Calendar event do you mean? Please include its title, "
            "time, or event ID in your latest message. Nothing was changed."
        )
        return {
            "assistant_response": response,
            "provider": "",
            "model": "",
            "messages": [
                {"role": "user", "content": state["user_message"]},
                {"role": "assistant", "content": response},
            ],
        }
    try:
        with SessionLocal() as db:
            service = CalendarService(db)
            if isinstance(calendar_name, str) and not calendar_id:
                calendars: list[dict[str, Any]] = []
                page_token = None
                seen_page_tokens: set[str] = set()
                calendar_list_complete = False
                for _ in range(CALENDAR_LIST_MAX_PAGES):
                    page = asyncio.run(
                        service.list_calendars(
                            user_id=UUID(str(user_id)),
                            connection_id=UUID(str(connection_id)),
                            max_results=250,
                            page_token=page_token,
                        )
                    )
                    page_calendars = page.get("calendars")
                    if not isinstance(page_calendars, list):
                        raise ValueError("Calendar returned invalid calendar results.")
                    calendars.extend(
                        item for item in page_calendars
                        if isinstance(item, dict)
                    )
                    next_page_token = page.get("next_page_token")
                    if not next_page_token:
                        calendar_list_complete = True
                        break
                    if (
                        not isinstance(next_page_token, str)
                        or next_page_token in seen_page_tokens
                    ):
                        raise ValueError("Calendar returned invalid pagination.")
                    seen_page_tokens.add(next_page_token)
                    page_token = next_page_token
                if not calendar_list_complete:
                    response = (
                        "I couldn’t safely complete the Calendar name "
                        "lookup. Nothing was changed."
                    )
                    return {
                        "assistant_response": response,
                        "provider": "",
                        "model": "",
                        "messages": [
                            {"role": "user", "content": state["user_message"]},
                            {"role": "assistant", "content": response},
                        ],
                    }
                matches = [
                    item for item in calendars
                    if isinstance(item.get("summary"), str)
                    and item["summary"].strip().casefold()
                    == calendar_name.strip().casefold()
                ]
                if len(matches) != 1 or not isinstance(matches[0].get("id"), str):
                    response = (
                        f"I couldn’t uniquely resolve the Calendar "
                        f"{calendar_name!r}. Please choose an accessible "
                        "calendar that matches exactly one name. Nothing was changed."
                    )
                    return {
                        "assistant_response": response,
                        "provider": "",
                        "model": "",
                        "messages": [
                            {"role": "user", "content": state["user_message"]},
                            {"role": "assistant", "content": response},
                        ],
                    }
                calendar_id = matches[0]["id"]
            selected = None
            generic_delete_discovery = bool(
                action_type == "calendar.delete_event"
                and delete_details
                and delete_details.get("generic_event_discovery")
                and not isinstance(event_id, str)
            )
            if not isinstance(event_id, str) or not event_id.strip():
                if (
                    not isinstance(event_query, str) or not event_query.strip()
                ) and not generic_delete_discovery:
                    raise ValueError("An event ID or event search is required.")
                lookup_time_min = (
                    lookup_details.get("lookup_time_min")
                    if lookup_details
                    else None
                )
                lookup_time_max = (
                    lookup_details.get("lookup_time_max")
                    if lookup_details
                    else None
                )
                matches: dict[str, Any] = {}
                events: list[dict[str, Any]] = []
                page_token = None
                seen_page_tokens: set[str] = set()
                for _ in range(CALENDAR_LIST_MAX_PAGES):
                    page = asyncio.run(
                        service.list_events(
                            user_id=UUID(str(user_id)),
                            connection_id=UUID(str(connection_id)),
                            calendar_id=calendar_id,
                            max_results=10,
                            page_token=page_token,
                            time_min=lookup_time_min,
                            time_max=lookup_time_max,
                            query=(None if generic_delete_discovery else event_query),
                            time_zone=(
                                lookup_details.get("time_zone")
                                if lookup_details
                                else state.get("user_timezone", "UTC")
                            ),
                        )
                    )
                    page_events = page.get("events")
                    if not isinstance(page_events, list):
                        raise ValueError(
                            "Calendar returned invalid event results."
                        )
                    events.extend(
                        event for event in page_events
                        if isinstance(event, dict)
                    )
                    matches = page
                    next_page_token = page.get("next_page_token")
                    if not next_page_token:
                        page_token = None
                        break
                    if (
                        not isinstance(next_page_token, str)
                        or next_page_token in seen_page_tokens
                    ):
                        raise ValueError(
                            "Calendar returned an invalid pagination token."
                        )
                    seen_page_tokens.add(next_page_token)
                    page_token = next_page_token
                matches = {**matches, "events": events}
                if page_token:
                    matches["next_page_token"] = page_token
                else:
                    matches["next_page_token"] = None
                if (
                    action_type == "calendar.update_event"
                    and update_details
                ) or (
                    action_type == "calendar.delete_event"
                    and delete_details
                    and (
                        delete_details.get("delete_entire_series")
                        or delete_details.get("event_title_is_explicit")
                    )
                ) or (
                    action_type == "calendar.delete_event"
                    and delete_details
                    and delete_details.get("event_date") is not None
                    and not delete_details.get("generic_event_discovery")
                ):
                    events = [
                        event for event in events
                        if isinstance(event, dict)
                        and _update_event_matches_request(
                            event,
                            event_query,
                            lookup_details or {},
                        )
                    ]
                if not events:
                    if generic_delete_discovery:
                        day_label = "that day"
                        if delete_details and delete_details.get("event_date") is not None:
                            try:
                                tz_name = state.get("user_timezone", "UTC") or "UTC"
                                req_t = state.get("request_time")
                                if req_t:
                                    now_dt = datetime.fromisoformat(str(req_t).replace("Z", "+00:00"))
                                else:
                                    now_dt = datetime.now(timezone.utc)
                                today_local = now_dt.astimezone(ZoneInfo(tz_name)).date()
                                ev_date = delete_details.get("event_date")
                                if ev_date == today_local:
                                    day_label = "today"
                                elif ev_date == today_local - timedelta(days=1):
                                    day_label = "yesterday"
                                elif ev_date == today_local + timedelta(days=1):
                                    day_label = "tomorrow"
                                else:
                                    day_label = ev_date.isoformat()
                            except Exception:
                                day_label = "that day"
                        response = f"I couldn't find any events on your calendar for {day_label}. Nothing was changed."
                    else:
                        response = (
                            "I couldn’t find an existing Calendar event matching "
                            "the requested title and date/time. Nothing was changed."
                        )
                    return {
                        "assistant_response": response,
                        "calendar_action_details": {
                            "calendar_id": calendar_id,
                            "event_query": event_query,
                        },
                        "provider": "",
                        "model": "",
                        "messages": [
                            {"role": "user", "content": state["user_message"]},
                            {"role": "assistant", "content": response},
                        ],
                    }
                if len(events) != 1 or matches.get("next_page_token"):
                    choice_events = [e for e in events[:5] if isinstance(e, dict)]
                    choices = "\n".join(
                        _format_event(
                            event,
                            state.get("user_timezone", "UTC"),
                            matches.get("calendar_id")
                            if isinstance(matches.get("calendar_id"), str)
                            else None,
                        )
                        for event in choice_events
                    )
                    user_msg_lower = str(state.get("user_message") or "").casefold()
                    bulk_delete_requested = bool(re.search(
                        r"\b(?:remove|delete|cancel)\s+(?:all|every|everything)\b|"
                        r"\b(?:remove|delete|cancel)\s+all\s+(?:the\s+)?(?:events?|meetings?|appointments?)\b|"
                        r"\ball\s+(?:the\s+)?(?:events?|meetings?|appointments?)\b",
                        user_msg_lower,
                    ))
                    if generic_delete_discovery:
                        day_label = "that day"
                        if delete_details and delete_details.get("event_date") is not None:
                            try:
                                tz_name = state.get("user_timezone", "UTC") or "UTC"
                                req_t = state.get("request_time")
                                if req_t:
                                    now_dt = datetime.fromisoformat(str(req_t).replace("Z", "+00:00"))
                                else:
                                    now_dt = datetime.now(timezone.utc)
                                today_local = now_dt.astimezone(ZoneInfo(tz_name)).date()
                                ev_date = delete_details.get("event_date")
                                if ev_date == today_local:
                                    day_label = "today"
                                elif ev_date == today_local - timedelta(days=1):
                                    day_label = "yesterday"
                                elif ev_date == today_local + timedelta(days=1):
                                    day_label = "tomorrow"
                                else:
                                    day_label = ev_date.isoformat()
                            except Exception:
                                day_label = "that day"
                        if (
                            action_type == "calendar.delete_event"
                            and bulk_delete_requested
                            and not matches.get("next_page_token")
                            and len(events) >= 2
                        ):
                            response = (
                                f"You asked to delete all {len(events)} events on {day_label}. "
                                "I will not delete anything without explicit confirmation.\n\n"
                                "Affected events:\n"
                                f"{choices}\n\n"
                                f"To proceed, reply with the exact confirmation: "
                                f"\"confirm delete all {len(events)} events\". "
                                "To cancel, reply \"cancel\". To delete only one event, reply with its number or title."
                            )
                        else:
                            response = (
                                f"I found multiple events on your calendar for {day_label}. "
                                "Which one do you want to delete? Please reply with the number (for example, \"delete the second one\") or title/time:\n\n"
                                f"{choices}"
                            )
                    else:
                        response = (
                            "I found multiple possible Calendar events. Which one "
                            "do you mean? Please reply with the number (for example, \"delete the second one\") or title/time:\n\n"
                            f"{choices}"
                        )
                    payload: dict[str, Any] = {
                        "assistant_response": response,
                        "calendar_action_details": {
                            "calendar_id": calendar_id,
                            "event_query": event_query,
                        },
                        "provider": "",
                        "model": "",
                        "messages": [
                            {"role": "user", "content": state["user_message"]},
                            {"role": "assistant", "content": response},
                        ],
                    }
                    if action_type == "calendar.delete_event" and not matches.get("next_page_token"):
                        req_time = state.get("request_time")
                        try:
                            now_dt = datetime.fromisoformat(str(req_time).replace("Z", "+00:00")) if req_time else datetime.now(timezone.utc)
                        except Exception:
                            now_dt = datetime.now(timezone.utc)
                        def _json_safe_event_boundary(value: Any) -> Any:
                            if isinstance(value, datetime):
                                return value.isoformat()
                            if isinstance(value, date):
                                return value.isoformat()
                            return value

                        candidates = []
                        for event in choice_events:
                            eid = event.get("id")
                            if not isinstance(eid, str) or not eid:
                                continue
                            start = event.get("start") if isinstance(event.get("start"), dict) else {}
                            end = event.get("end") if isinstance(event.get("end"), dict) else {}
                            candidates.append({
                                "event_id": eid,
                                "calendar_id": calendar_id,
                                "summary": event.get("summary"),
                                "start": _json_safe_event_boundary(start.get("date_time") or start.get("date")),
                                "end": _json_safe_event_boundary(end.get("date_time") or end.get("date")),
                                "time_zone": start.get("time_zone") or end.get("time_zone") or state.get("user_timezone", "UTC"),
                            })
                        if candidates:
                            pending_selection = {
                                "version": 1,
                                "operation": "delete_selection",
                                "user_id": str(state.get("user_id") or user_id),
                                "conversation_id": str(state.get("conversation_id") or ""),
                                "connection_id": str(connection_id),
                                "calendar_id": calendar_id,
                                "candidates": candidates,
                                "created_at": now_dt.isoformat(),
                                "expires_at": (now_dt + timedelta(minutes=10)).isoformat(),
                                "original_request": state.get("user_message"),
                                "generic_event_discovery": bool(generic_delete_discovery),
                            }
                            if (
                                action_type == "calendar.delete_event"
                                and bulk_delete_requested
                                and generic_delete_discovery
                                and not matches.get("next_page_token")
                                and len(candidates) >= 2
                            ):
                                bound_ids = [c.get("event_id") for c in candidates if isinstance(c.get("event_id"), str)]
                                pending_selection.update({
                                    "awaiting_bulk_confirmation": True,
                                    "bulk_candidate_event_ids": bound_ids,
                                    "bulk_candidate_count": len(candidates),
                                    "bulk_confirmation_phrase": f"confirm delete all {len(candidates)} events",
                                })
                            payload["pending_calendar_delete_selection"] = pending_selection
                    return payload
                selected = events[0]
                event_id = selected.get("id") if isinstance(selected, dict) else None
                if not isinstance(event_id, str) or not event_id:
                    raise ValueError("Calendar returned an invalid event.")
                if (
                    action_type == "calendar.delete_event"
                    and delete_details
                    and delete_details.get("delete_entire_series")
                ):
                    recurring_master_id = selected.get("recurring_event_id")
                    if (
                        not isinstance(recurring_master_id, str)
                        and selected.get("recurrence")
                    ):
                        recurring_master_id = event_id
                    if not isinstance(recurring_master_id, str):
                        response = (
                            "I found the matching event, but could not verify "
                            "its recurring master. Nothing was changed."
                        )
                        return {
                            "assistant_response": response,
                            "provider": "",
                            "model": "",
                            "messages": [
                                {"role": "user", "content": state["user_message"]},
                                {"role": "assistant", "content": response},
                            ],
                        }
                    master = asyncio.run(
                        service.get_event(
                            user_id=UUID(str(user_id)),
                            connection_id=UUID(str(connection_id)),
                            calendar_id=calendar_id,
                            event_id=recurring_master_id,
                        )
                    )
                    if not _matches_recurring_master(
                        master,
                        selected,
                        event_query,
                        delete_details,
                    ):
                        response = (
                            "The matching event did not verify as the "
                            "requested recurring series. Nothing was changed."
                        )
                        return {
                            "assistant_response": response,
                            "provider": "",
                            "model": "",
                            "messages": [
                                {"role": "user", "content": state["user_message"]},
                                {"role": "assistant", "content": response},
                            ],
                        }
                    event_id = recurring_master_id

            event = asyncio.run(
                service.validate_event_write_access(
                    user_id=UUID(str(user_id)),
                    connection_id=UUID(str(connection_id)),
                    calendar_id=calendar_id,
                    event_id=event_id,
                )
            )
            if (
                action_type == "calendar.update_event"
                and update_details
                and not _update_event_matches_request(
                    event,
                    event_query if isinstance(event_query, str) else None,
                    update_details,
                )
            ):
                response = (
                    "The selected Calendar event does not match the "
                    "requested title and date/time. Nothing was changed."
                )
                return {
                    "assistant_response": response,
                    "provider": "",
                    "model": "",
                    "messages": [
                        {"role": "user", "content": state["user_message"]},
                        {"role": "assistant", "content": response},
                    ],
                }
            if (
                action_type == "calendar.delete_event"
                and delete_details
                and delete_details.get("delete_entire_series")
                and not _matches_recurring_master(
                    event,
                    selected,
                    event_query,
                    delete_details,
                )
            ):
                response = (
                    "The recurring master changed or no longer matches the "
                    "requested series. Nothing was changed."
                )
                return {
                    "assistant_response": response,
                    "provider": "",
                    "model": "",
                    "messages": [
                        {"role": "user", "content": state["user_message"]},
                        {"role": "assistant", "content": response},
                    ],
                }
            if (
                event.get("recurrence")
                and not event.get("recurring_event_id")
                and not (
                    action_type == "calendar.delete_event"
                    and delete_details
                    and delete_details.get("delete_entire_series")
                )
            ):
                response = (
                    "This is a recurring Calendar series. Series-wide changes "
                    "are not available yet; nothing was changed."
                )
                return {
                    "assistant_response": response,
                    "provider": "",
                    "model": "",
                    "messages": [
                        {"role": "user", "content": state["user_message"]},
                        {"role": "assistant", "content": response},
                    ],
                }
            snapshot = _event_snapshot(event)
            if not isinstance(snapshot.get("etag"), str) or not snapshot["etag"]:
                raise ValueError("Calendar event has no concurrency token.")
            if action_type == "calendar.update_event":
                if not isinstance(changes, dict):
                    raise ValueError("Event changes are required.")
                changes = dict(changes)
                event_timezone = (
                    update_details.get("time_zone")
                    if update_details
                    else None
                ) or state.get("user_timezone", "UTC")
                changes, time_zone = _resolve_relative_time_changes(
                    event,
                    changes,
                    event_timezone,
                )
                if time_zone is None and "start" in changes:
                    for endpoint in ("start", "end"):
                        event_time = changes.get(endpoint)
                        if isinstance(event_time, dict) and "date" not in event_time:
                            event_time.setdefault("timeZone", event_timezone)
                    time_zone = event_timezone
                parameters = CalendarUpdateEventParameters.model_validate({
                    "calendar_id": calendar_id,
                    "event_id": event_id,
                    "expected_etag": snapshot["etag"],
                    "current_event": snapshot,
                    "changes": changes,
                    "time_zone": time_zone,
                })
            else:
                parameters = CalendarDeleteEventParameters.model_validate({
                    "calendar_id": calendar_id,
                    "event_id": event_id,
                    "expected_etag": snapshot["etag"],
                    "current_event": snapshot,
                    "delete_entire_series": bool(
                        delete_details
                        and delete_details.get("delete_entire_series")
                    ),
                })
    except ValidationError:
        response = (
            "I couldn’t validate the requested Calendar change. Please clarify "
            "the event and the exact change. Nothing was changed."
        )
    except ValueError:
        response = (
            "I couldn’t verify the recurring Calendar master. Nothing was "
            "changed; please check the event and account, then try again."
            if (
                action_type == "calendar.delete_event"
                and delete_details
                and delete_details.get("delete_entire_series")
            )
            else (
                "I couldn’t safely apply the requested time change to this "
                "event. Please clarify the time and timezone. Nothing was "
                "changed."
            )
        )
    except PermissionError:
        response = (
            "The selected Google account is missing Calendar write permission. "
            "Grant access for that account in Connected Apps. Nothing was changed."
        )
    except Exception as exc:
        logger.exception(
            "Calendar mutation proposal preflight failed | user_id=%s "
            "| error_type=%s",
            user_id,
            type(exc).__name__,
        )
        response = (
            "I couldn’t verify the selected Calendar event. Nothing was "
            "changed; please check the event and account, then try again."
        )
    else:
        event_title = event.get("summary") or "Untitled event"
        verb = "update" if action_type == "calendar.update_event" else "delete"
        details = ""
        if action_type == "calendar.update_event":
            current = parameters.current_event
            labels = {
                "summary": "Title",
                "description": "Description",
                "location": "Location",
                "start": "Start",
                "end": "End",
                "attendees": "Attendees",
            }

            def display_value(value: object) -> str:
                if isinstance(value, dict):
                    return str(
                        value.get("dateTime")
                        or value.get("date")
                        or value
                    )
                if isinstance(value, list):
                    return ", ".join(str(item) for item in value) or "none"
                return str(value) if value is not None else "none"

            changes_summary = [
                f"- {labels[key]}: "
                f"{display_value(current.get(key))} → "
                f"{display_value(value)}"
                for key, value in parameters.changes.items()
                if key in labels
            ]
            if changes_summary:
                details = (
                    "\n\nBefore → after:\n" + "\n".join(changes_summary)
                )
        else:
            event_start = _format_event_time(
                event.get("start"),
                state.get("user_timezone", "UTC"),
            )
            event_end = _format_event_time(
                event.get("end"),
                state.get("user_timezone", "UTC"),
            )
            details = f" ({event_start} to {event_end})"
            if parameters.delete_entire_series:
                details = (
                    " (entire daily recurring series with no end date, "
                    f"including all future occurrences; {event_start} to "
                    f"{event_end})"
                )
        response = (
            f"I prepared an approval request to {verb} **{event_title}**"
            f"{details}. "
            "Review the approval card below. Google Calendar will not be "
            "changed unless you approve and separately execute the request."
        )
        return {
            "assistant_response": response,
            "provider": "",
            "model": "",
            "calendar_action_proposal": {
                "action_type": action_type,
                "parameters": parameters.model_dump(mode="json"),
            },
            "messages": [
                {"role": "user", "content": state["user_message"]},
                {"role": "assistant", "content": response},
            ],
        }

    return {
        "assistant_response": response,
        "provider": "",
        "model": "",
        "messages": [
            {"role": "user", "content": state["user_message"]},
            {"role": "assistant", "content": response},
        ],
    }


def _pending_delete_title_clarification(state: ChatState) -> str | None:
    """Return a plain event-title answer for a pending Delete clarification.

    The continuation marker is added internally by the intent router.  Keeping
    this separate from the original request prevents a generic phrase such as
    "Delete my event" from being mistaken for the actual event title.
    """
    pending = state.get("pending_calendar_action")
    action_request = state.get("calendar_action_request")
    if (
        not isinstance(pending, dict)
        or pending.get("operation") != "delete"
        or not isinstance(action_request, str)
        or not action_request.strip()
    ):
        return None

    matches = re.findall(
        r"(?im)^\s*Clarification supplied by the user:\s*(.*?)\s*$",
        action_request,
    )
    if not matches:
        return None

    title = matches[-1].strip().strip(' \t\"\'“”')
    title = re.sub(r"[.?!,;]+$", "", title).strip()
    if not title or len(title) > 250:
        return None

    normalized = " ".join(title.casefold().split())
    generic_values = {
        "event", "my event", "the event", "calendar event",
        "meeting", "my meeting", "the meeting",
        "appointment", "my appointment", "the appointment",
        "that event", "this event",
    }
    if normalized in generic_values:
        return None

    # Date/time-only answers belong to occurrence disambiguation, not the
    # event-title field.  Do not turn answers such as "tomorrow" or "10 AM"
    # into a Calendar search query.
    temporal_only = re.fullmatch(
        r"(?:today|tomorrow|yesterday|tonight|this\s+week|next\s+week|"
        r"this\s+month|next\s+month|monday|tuesday|wednesday|thursday|"
        r"friday|saturday|sunday|"
        r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|"
        r"jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|"
        r"nov(?:ember)?|dec(?:ember)?)\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?|"
        r"\d{1,2}(?:st|nd|rd|th)?\s+(?:jan(?:uary)?|feb(?:ruary)?|"
        r"mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|"
        r"sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)(?:\s+\d{4})?|"
        r"\d{4}-\d{2}-\d{2}|"
        r"\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)?"
        r"(?:\s*(?:to|[-–—])\s*\d{1,2}(?::\d{2})?\s*"
        r"(?:a\.?m\.?|p\.?m\.?)?)?)",
        normalized,
        re.IGNORECASE,
    )
    if temporal_only:
        return None

    # A clarification must identify the event, not replace the pending action
    # with a cancellation or a different instruction.
    if re.match(
        r"^(?:never\s*mind|nevermind|stop|cancel|delete|remove|update|change)\b",
        normalized,
    ):
        return None
    return title


def calendar_agent_node(
    state: ChatState,
    runtime: Runtime[ChatContext],
) -> dict[str, Any]:
    emit_status("Checking your calendar...")
    plan = state.get("gmail_plan") or {}
    if state.get("calendar_passthrough_response") and isinstance(
        state.get("assistant_response"), str
    ):
        response = state["assistant_response"]
        out: dict[str, Any] = {
            "assistant_response": response,
            "provider": state.get("provider", ""),
            "model": state.get("model", ""),
            "messages": [
                {"role": "user", "content": state.get("user_message", "")},
                {"role": "assistant", "content": response},
            ],
        }
        # Preserve pending-state updates emitted by router (e.g. bulk confirmation binding).
        for key in (
            "pending_calendar_delete_selection",
            "pending_calendar_action",
            "pending_calendar_read",
            "pending_contacts_clarification",
            "calendar_action_details",
        ):
            if key in state:
                out[key] = state.get(key)
        return out
    if plan.get("intent") == "calendar_action_cancelled":
        response = (
            "The pending Google Calendar request was canceled. "
            "No Calendar action was prepared or executed."
        )
        return {
            "assistant_response": response,
            "provider": "",
            "model": "",
            "messages": [
                {"role": "user", "content": state["user_message"]},
                {"role": "assistant", "content": response},
            ],
        }
    if plan.get("intent") == "approval_workflow":
        pending = state.get("pending_calendar_create_proposal")
        operation = plan.get("operation")
        if isinstance(pending, dict) and operation in {"approve", "reject"}:
            proposal_id = pending.get("proposal_id")
            user_id = state.get("user_id")
            if proposal_id and user_id:
                try:
                    with SessionLocal() as db:
                        service = ActionApprovalService(db)
                        proposal = (
                            service.approve(
                                user_id=UUID(str(user_id)),
                                proposal_id=UUID(str(proposal_id)),
                            )
                            if operation == "approve"
                            else service.reject(
                                user_id=UUID(str(user_id)),
                                proposal_id=UUID(str(proposal_id)),
                            )
                        )
                    response = (
                        "The Calendar event proposal was approved. "
                        "No Calendar mutation was executed."
                        if operation == "approve"
                        else "The Calendar event proposal was rejected. "
                        "No Calendar mutation was executed."
                    )
                    return {
                        "assistant_response": response,
                        "provider": "",
                        "model": "",
                        "calendar_approval_result": proposal.model_dump(
                            mode="json"
                        ),
                        "messages": [
                            {"role": "user", "content": state["user_message"]},
                            {"role": "assistant", "content": response},
                        ],
                    }
                except Exception as exc:
                    logger.warning(
                        "Calendar chat approval failed | proposal_id=%s "
                        "| operation=%s | error_type=%s",
                        proposal_id,
                        operation,
                        type(exc).__name__,
                    )
        response = (
            "Use the Execute action on the approved Calendar proposal card. "
            "Execution is separate and chat confirmation does not execute "
            "a Calendar mutation."
            if operation == "execute"
            else "I couldn’t approve or reject that Calendar proposal safely. "
            "Use the action card and try again."
        )
        return {
            "assistant_response": response,
            "provider": "",
            "model": "",
            "messages": [
                {"role": "user", "content": state["user_message"]},
                {"role": "assistant", "content": response},
            ],
        }
    action_request = state.get("calendar_action_request")
    if isinstance(action_request, str) and action_request.strip():
        state = {**state, "user_message": action_request}
    tool_name = plan.get("calendar_tool")
    arguments = plan.get("calendar_arguments")
    update_details = None
    current_delete_request = _is_calendar_delete_request(
        state["user_message"]
    )
    current_update_request = (
        not current_delete_request
        and _is_calendar_update_request(state["user_message"])
    )
    if current_delete_request:
        tool_name = "delete_calendar_event_proposal"
    elif current_update_request:
        tool_name = "update_calendar_event_proposal"
        planner_arguments = plan.get("calendar_arguments")
        log_calendar_update_diagnostic(
            boundary="calendar_agent_preparse",
            message_length=len(state["user_message"]),
            timezone_present="Asia/Kolkata" in state["user_message"],
            planner_intent=str(plan.get("intent", "-")),
            planner_tool=str(plan.get("calendar_tool", "-")),
            planner_arguments_null=not isinstance(
                planner_arguments,
                dict,
            ),
            planner_timezone=(
                planner_arguments.get("time_zone")
                if isinstance(planner_arguments, dict)
                else None
            ),
            selected_route="google_calendar",
            module_path=__file__,
        )
    if current_update_request:
        arguments, update_details = merge_calendar_update_arguments(
            state["user_message"],
            arguments,
            default_time_zone=state.get("user_timezone"),
            reference_time=(
                _parse_request_time(
                    state.get("request_time") or datetime.now(UTC)
                )
            ),
        )
        tool_name = "update_calendar_event_proposal"
    delete_details = None
    if current_delete_request:
        planner_arguments = arguments if isinstance(arguments, dict) else {}
        planner_time_zone = planner_arguments.get("time_zone")
        if planner_time_zone is None:
            planner_time_zone = state.get("user_timezone")
        delete_details = parse_calendar_delete_request(
            state["user_message"],
            planner_time_zone=planner_time_zone,
            reference_time=state.get("request_time"),
        )
        arguments = dict(planner_arguments)
        if delete_details.get("event_query"):
            arguments["event_query"] = delete_details["event_query"]

        # Resolve contextual references such as "the event I just created"
        # using the most recent trusted Calendar creation result in this
        # conversation. This never trusts an event id from LLM text alone.
        contextual_proposal, contextual_event_id = (
            _recent_created_calendar_event(state)
        )
        current_query = arguments.get("event_query")
        query_text = (
            " ".join(str(current_query).casefold().split())
            if isinstance(current_query, str)
            else ""
        )
        recent_summary = None
        if isinstance(contextual_proposal, dict):
            recent_params = contextual_proposal.get("parameters")
            if isinstance(recent_params, dict):
                candidate_summary = recent_params.get("summary")
                if isinstance(candidate_summary, str):
                    recent_summary = candidate_summary.strip()
        recent_title = (
            " ".join(recent_summary.casefold().split())
            if recent_summary
            else ""
        )
        generic_context_query = query_text in {
            "this",
            "that",
            "it",
            "one",
            "the one",
            "event",
            "my event",
            "the event",
            "this event",
            "that event",
            "calendar event",
            "meeting",
            "my meeting",
            "the meeting",
            "appointment",
            "my appointment",
            "the appointment",
        }
        explicit_context_reference = bool(re.search(
            r"\b(?:just created|you (?:just )?created|"
            r"i just created|we just (?:created|made)|"
            r"that event|this event)\b",
            state["user_message"],
            re.IGNORECASE,
        ))
        matches_recent_title = bool(
            query_text
            and recent_title
            and (
                query_text == recent_title
                or query_text in recent_title
                or recent_title in query_text
            )
        )
        if (
            contextual_event_id is not None
            and not (
                isinstance(arguments.get("event_id"), str)
                and str(arguments.get("event_id")).strip()
            )
            and (
                not query_text
                or generic_context_query
                or explicit_context_reference
                or matches_recent_title
            )
        ):
            arguments["event_id"] = contextual_event_id
            if isinstance(contextual_proposal, dict):
                params = contextual_proposal.get("parameters")
                if isinstance(params, dict):
                    if (
                        not isinstance(arguments.get("calendar_id"), str)
                        or not arguments.get("calendar_id")
                    ) and isinstance(params.get("calendar_id"), str):
                        arguments["calendar_id"] = params["calendar_id"]
                    if (
                        not isinstance(arguments.get("time_zone"), str)
                        or not arguments.get("time_zone")
                    ) and isinstance(params.get("time_zone"), str):
                        arguments["time_zone"] = params["time_zone"]
                    if not isinstance(arguments.get("event_query"), str):
                        summary = params.get("summary")
                        if isinstance(summary, str) and summary.strip():
                            arguments["event_query"] = summary
            state = {
                **state,
                "trusted_calendar_event_ids": {contextual_event_id},
            }

        clarification_title = _pending_delete_title_clarification(state)
        current_query = arguments.get("event_query")
        generic_queries = {
            "event", "my event", "the event", "calendar event",
            "meeting", "my meeting", "the meeting",
            "appointment", "my appointment", "the appointment",
        }
        if clarification_title and (
            not isinstance(current_query, str)
            or " ".join(current_query.casefold().split()) in generic_queries
        ):
            arguments["event_query"] = clarification_title
        if delete_details.get("calendar_id"):
            arguments["calendar_id"] = delete_details["calendar_id"]
            arguments.pop("calendar_name", None)
        elif delete_details.get("calendar_name"):
            arguments["calendar_name"] = delete_details["calendar_name"]
            arguments.pop("calendar_id", None)
        tool_name = "delete_calendar_event_proposal"
    next_event_details = (
        plan.get("intent") == "google_calendar"
        and _is_next_event_details_request(state["user_message"])
    )
    next_event_request_time = None
    next_event_timezone = None
    if next_event_details:
        tool_name = "list_calendar_events"
        arguments = dict(arguments) if isinstance(arguments, dict) else {}
        context_request_time = runtime.context.get("request_time")
        next_event_request_time = _parse_request_time(
            context_request_time
            or state.get("request_time")
            or arguments.get("time_min")
            or datetime.now(timezone.utc)
        )
        next_event_timezone = (
            state.get("user_timezone")
            or arguments.get("time_zone")
            or "UTC"
        )
        next_event_zone = ZoneInfo(next_event_timezone)
        arguments.update({
            "time_min": next_event_request_time.astimezone(
                next_event_zone
            ).isoformat(),
            "time_zone": next_event_timezone,
            "max_results": 10,
        })
    if (
        tool_name is not None
        and runtime.context.get("calendar_enabled", True) is False
    ):
        response = (
            "Google Calendar is turned off in Connected Apps. Turn it on "
            "there to use Calendar; your Google account remains connected."
        )
        return {
            "assistant_response": response,
            "provider": "",
            "model": "",
            "messages": [
                {"role": "user", "content": state["user_message"]},
                {"role": "assistant", "content": response},
            ],
        }
    if tool_name == "verify_calendar_proposal_event":
        parameters = arguments.get("parameters") if isinstance(arguments, dict) else None
        event_connection_id = runtime.context.get("calendar_event_connection_id")
        user_id = runtime.context.get("user_id")
        if not event_connection_id or not user_id:
            response = (
                "Live verification unavailable: Calendar read access or a "
                "connected Google account is unavailable."
            )
        elif not isinstance(parameters, dict):
            response = (
                "Live verification unavailable: the saved proposal does not "
                "contain enough event details."
            )
        else:
            try:
                start = parameters.get("start")
                end = parameters.get("end")
                summary = parameters.get("summary")
                zone_name = parameters.get("time_zone") or "UTC"
                start_dt = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
                end_dt = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
                with SessionLocal() as db:
                    result = asyncio.run(
                        CalendarService(db).list_events(
                            user_id=UUID(str(user_id)),
                            connection_id=UUID(str(event_connection_id)),
                            calendar_id=str(parameters.get("calendar_id") or "primary"),
                            time_min=start_dt,
                            time_max=end_dt,
                            query=str(summary) if summary else None,
                            time_zone=zone_name,
                            max_results=100,
                        )
                    )
                events = result.get("events") if isinstance(result, dict) else None
                title_matches: list[dict[str, Any]] = []
                if isinstance(events, list):
                    for event in events:
                        if not isinstance(event, dict):
                            continue
                        event_summary = str(event.get("summary") or "").strip()
                        if event_summary.casefold() == str(summary or "").strip().casefold():
                            title_matches.append(event)

                if not title_matches:
                    response = (
                        f"No matching {summary} event was found in Google Calendar "
                        f"for the saved proposal's {start_dt.isoformat()}–"
                        f"{end_dt.isoformat()} {zone_name} time window."
                    )
                else:
                    exact_match = False
                    for event in title_matches:
                        event_start = (
                            (event.get("start") or {}).get("date_time")
                            if isinstance(event.get("start"), dict)
                            else event.get("start")
                        )
                        event_end = (
                            (event.get("end") or {}).get("date_time")
                            if isinstance(event.get("end"), dict)
                            else event.get("end")
                        )
                        try:
                            actual_start = datetime.fromisoformat(
                                str(event_start).replace("Z", "+00:00")
                            )
                            actual_end = datetime.fromisoformat(
                                str(event_end).replace("Z", "+00:00")
                            )
                            starts_match = (
                                actual_start.astimezone(UTC).replace(microsecond=0)
                                == start_dt.astimezone(UTC).replace(microsecond=0)
                            )
                            ends_match = (
                                actual_end.astimezone(UTC).replace(microsecond=0)
                                == end_dt.astimezone(UTC).replace(microsecond=0)
                            )
                        except (TypeError, ValueError):
                            starts_match = False
                            ends_match = False
                        if starts_match and ends_match:
                            exact_match = True
                            break
                    if exact_match:
                        response = (
                            f"Event found in Google Calendar: a matching {summary} event "
                            "was found at the saved proposal's date and time."
                        )
                    else:
                        response = (
                            f"A {summary} event was found in Google Calendar, but its "
                            "title, start time, or end time differs from the saved proposal."
                        )
            except Exception as exc:
                logger.warning(
                    "Calendar proposal verification failed | proposal_id=%s | error_type=%s",
                    arguments.get("proposal_id") if isinstance(arguments, dict) else None,
                    type(exc).__name__,
                )
                response = (
                    "Live verification unavailable: Google Calendar could not "
                    "be queried. Try again after checking the Calendar connection."
                )
        return {
            "assistant_response": response,
            "provider": "",
            "model": "",
            "messages": [
                {"role": "user", "content": state["user_message"]},
                {"role": "assistant", "content": response},
            ],
        }
    if tool_name == "get_calendar_proposal_status":
        proposal_status = arguments.get("status") if isinstance(arguments, dict) else None
        proposal_status = (
            str(proposal_status).lower()
            if proposal_status is not None
            else "unknown"
        )
        approval_text = (
            "Approved"
            if proposal_status == "approved"
            else proposal_status.capitalize()
        )
        execution_available = bool(
            (state.get("assistant_capabilities", {}).get("google_calendar", {})
             .get("create_execution_available", False))
        )
        execution_text = "Available" if execution_available else "Disabled"
        execution_attempted = bool(
            arguments.get("execution_started_at")
            or arguments.get("completed_at")
            or arguments.get("calendar_event_id")
        )
        provider_confirmation = bool(
            arguments.get("calendar_event_id")
        )
        lines = [
            f"Approval = {approval_text}",
            f"Execution = {execution_text}",
            f"Execution attempted = {'Yes' if execution_attempted else 'No'}",
            f"Provider confirmation = {'Present' if provider_confirmation else 'None'}",
            "Approval does not prove that the event exists in Google Calendar.",
        ]
        response = "\n".join(lines)
        return {
            "assistant_response": response,
            "provider": "",
            "model": "",
            "messages": [
                {"role": "user", "content": state["user_message"]},
                {"role": "assistant", "content": response},
            ],
        }
    if tool_name in {
        "update_calendar_event_proposal",
        "delete_calendar_event_proposal",
    }:
        return _prepare_event_mutation(
            state,
            runtime,
            arguments,
            "calendar.update_event"
            if tool_name == "update_calendar_event_proposal"
            else "calendar.delete_event",
            update_details=update_details,
            delete_details=delete_details,
        )
    if (
        tool_name == "clarify"
        and state.get("gmail_plan", {}).get("intent") == "google_calendar"
        and re.search(
            r"\b(?:move|reschedule|update|edit|change|modify)\b",
            state["user_message"],
            re.IGNORECASE,
        )
    ):
        return _prepare_event_mutation(
            state,
            runtime,
            arguments,
            "calendar.update_event",
        )
    if tool_name == "clarify" or (
        not isinstance(arguments, dict)
        and tool_name != "create_calendar_event_proposal"
    ):
        generic_overview = (
            state.get("gmail_plan", {}).get("intent") == "google_calendar"
            and (
                re.fullmatch(
                    r"\s*(?:my\s+)?(?:google\s+)?calendar\s*[.!?]*\s*",
                    state["user_message"].strip(),
                    re.IGNORECASE,
                )
                or re.fullmatch(
                    r"\s*(?:please\s+)?(?:check|show|open|view)\s+"
                    r"(?:my\s+)?(?:google\s+)?calendar\s*[.!?]*\s*",
                    state["user_message"].strip(),
                    re.IGNORECASE,
                )
                or re.fullmatch(
                    r"\s*(?:what(?:'s| is)\s+on\s+)?(?:my\s+)?"
                    r"(?:google\s+)?calendar\s*[.!?]*\s*",
                    state["user_message"].strip(),
                    re.IGNORECASE,
                )
                or re.fullmatch(
                    r"\s*show\s+my\s+calendar\s*[.!?]*\s*",
                    state["user_message"].strip(),
                    re.IGNORECASE,
                )
            )
        )
        if generic_overview:
            tool_name = "get_today_schedule"
            arguments = {
                "time_zone": state.get("user_timezone") or "UTC",
                "calendar_id": "primary",
            }
            # continue to read-tool execution below
        else:
            if (
                re.search(
                    r"\b(?:create|add|book|schedule|move|update|edit|change|"
                    r"modify|reschedule|delete|remove|cancel)\b",
                    state["user_message"],
                    re.IGNORECASE,
                )
                and state.get("gmail_plan", {}).get("intent") == "google_calendar"
            ):
                if runtime.context.get("calendar_enabled", True) is False:
                    response = (
                        "Google Calendar is turned off in Connected Apps. Turn it "
                        "on there to use Calendar; your Google account remains connected."
                    )
                elif not runtime.context.get("google_connection_available", False):
                    response = (
                        "Google Calendar is not connected. Connect a Google "
                        "account in Connected Apps. No event was created."
                    )
                else:
                    response = _clarification(state)
            else:
                response = _clarification(state)

            pending_read: dict[str, Any] | None = None
            try:
                message_text = (state.get("user_message") or "").strip()
                lowered_msg = message_text.casefold()
                is_month_like_read = bool(
                    re.search(
                        r"\b(this|next|current)\s+months?\b|"
                        r"\b(?:this|next|current)\s+month(?:['’]s)?\b|"
                        r"\b(january|february|march|april|may|june|july|august|"
                        r"september|october|november|december)\b",
                        lowered_msg,
                    )
                    and re.search(r"\b(?:calendar|schedule|events?|meetings?)\b", lowered_msg)
                    and not re.search(
                        r"\b(?:create|add|book|schedule|move|update|edit|change|"
                        r"modify|reschedule|delete|remove|cancel)\b",
                        lowered_msg,
                    )
                )
                if is_month_like_read:
                    timezone_name = state.get("user_timezone") or "UTC"
                    try:
                        ZoneInfo(timezone_name)
                    except Exception:
                        timezone_name = "UTC"
                    req_time = state.get("request_time")
                    if isinstance(req_time, datetime):
                        now_dt = req_time
                    elif isinstance(req_time, str):
                        now_dt = datetime.fromisoformat(req_time.replace("Z", "+00:00"))
                    else:
                        now_dt = datetime.now(timezone.utc)
                    today_local = now_dt.astimezone(ZoneInfo(timezone_name)).date()
                    rel = re.search(
                        r"\b(this|next|current)\s+months?\b|\b(?:this|next|current)\s+month(?:['’]s)?\b",
                        lowered_msg,
                    )
                    named = re.search(
                        r"\b(january|february|march|april|may|june|july|august|"
                        r"september|october|november|december)\b(?:\s+(20\d{2}))?\b",
                        lowered_msg,
                    )
                    month_start: date | None = None
                    if rel:
                        token_match = re.search(r"\b(this|next|current)\b", rel.group(0), re.IGNORECASE)
                        token = token_match.group(1).casefold() if token_match else "this"
                        offset = 1 if token == "next" else 0
                        month_start = date(
                            today_local.year + (today_local.month - 1 + offset) // 12,
                            (today_local.month - 1 + offset) % 12 + 1,
                            1,
                        )
                    elif named:
                        month_names = (
                            "january", "february", "march", "april", "may", "june",
                            "july", "august", "september", "october", "november", "december",
                        )
                        month_num = month_names.index(named.group(1).casefold()) + 1
                        month_year = int(named.group(2)) if named.group(2) else today_local.year
                        month_start = date(month_year, month_num, 1)
                    if month_start is not None:
                        preserved_arguments = {
                            "date": month_start.isoformat(),
                            "period": "month",
                            "time_zone": timezone_name,
                            "calendar_id": "primary",
                        }
                        pending_read = {
                            "version": 1,
                            "type": "calendar_read_continuation",
                            "user_id": str(state.get("user_id")),
                            "conversation_id": str(state.get("conversation_id")),
                            "expires_at": (now_dt + timedelta(minutes=10)).isoformat(),
                            "calendar_id": "primary",
                            "time_zone": timezone_name,
                            "last_tool": "get_calendar_schedule",
                            "last_request": message_text,
                            "intent": "google_calendar",
                            "tool": "get_calendar_schedule",
                            "arguments": preserved_arguments,
                        }
            except Exception:
                pending_read = None

            result = {
                "assistant_response": response,
                "provider": "",
                "model": "",
                "messages": [
                    {"role": "user", "content": state["user_message"]},
                    {"role": "assistant", "content": response},
                ],
            }
            if pending_read is not None:
                result["pending_calendar_read"] = pending_read
            return result


    def _create_event_proposal(
        state: ChatState,
        runtime: Runtime[ChatContext],
        arguments: object,
    ) -> dict[str, Any]:
        context = runtime.context
        capabilities = (
            (state.get("assistant_capabilities") or {}).get("google_calendar", {})
        )
        if not context.get("google_connection_available", False):
            response = (
                "Google Calendar is not connected. Connect a Google account "
                "in Connected Apps. No event was created."
            )
            return {
                "assistant_response": response,
                "provider": "",
                "model": "",
                "messages": [
                    {"role": "user", "content": state["user_message"]},
                    {"role": "assistant", "content": response},
                ],
            }
        connection_id = context.get("calendar_write_connection_id")
        user_id = context.get("user_id")
        if not connection_id or not user_id:
            if not user_id:
                response = "Calendar access is unavailable for this request. Nothing was created."
            elif not context.get("google_connection_available", False):
                response = (
                    "Google Calendar is not connected. Connect a Google account "
                    "in Connected Apps. No event was created."
                )
            elif context.get("calendar_default_connection_invalid", False):
                response = (
                    "Your saved default Google Calendar account is unavailable. "
                    "Choose a connected Google Calendar account in Connected Apps, "
                    "then try again. Nothing was created."
                )
            elif context.get("calendar_write_selection_required", False):
                response = (
                    "More than one connected Google account can create Calendar "
                    "events, and no valid default is selected. Choose your default "
                    "Google Calendar account in Connected Apps, then try again. "
                    "Nothing was created."
                )
            elif context.get("calendar_selected_write_scope_missing", False):
                response = (
                    "Your selected Google account is missing the required Calendar "
                    "event write permission (calendar.events). Re-consent that "
                    "account in Connected Apps for Calendar event changes. I will "
                    "not switch to another account. Nothing was created."
                )
            elif not capabilities.get("create_feature_enabled", False):
                response = (
                    "Calendar event creation is currently unavailable in this "
                    "environment. Your Google account was not used and nothing was "
                    "created."
                )
            elif not capabilities.get("write_permission_granted", False):
                response = (
                    "No eligible connected Google Calendar account with Calendar "
                    "event write permission is available for this user. Grant "
                    "Calendar event write access (calendar.events) in Connected "
                    "Apps, then try again. Nothing was created."
                )
            else:
                response = (
                    "I couldn’t safely select an authorized Google Calendar account "
                    "for this event. Please verify your connected Google Calendar "
                    "account permissions and default selection in Connected Apps. "
                    "Nothing was created."
                )
            return {
                "assistant_response": response,
                "provider": "",
                "model": "",
                "messages": [
                    {"role": "user", "content": state["user_message"]},
                    {"role": "assistant", "content": response},
                ],
            }
        draft_data = state.get("calendar_create_draft")
        if isinstance(draft_data, dict):
            try:
                draft = CalendarCreateDraft.model_validate(draft_data)
            except ValidationError:
                response = (
                    "I couldn’t safely validate the saved Calendar draft. "
                    "Please provide the event details again. Nothing was created."
                )
                return {
                    "assistant_response": response,
                    "provider": "",
                    "model": "",
                    "calendar_create_draft": None,
                    "messages": [
                        {"role": "user", "content": state["user_message"]},
                        {"role": "assistant", "content": response},
                    ],
                }

            draft_json = draft.model_dump(mode="json", exclude_none=True)
            if not draft.invalid_fields:
                draft_json.pop("invalid_fields", None)
            missing = draft_missing_fields(draft_json)
            non_calendar_missing = [
                field for field in missing if field != "calendar"
            ]
            if draft.invalid_fields or non_calendar_missing:
                response = _calendar_create_missing_details_prompt(
                    draft.invalid_fields or non_calendar_missing
                )
                return {
                    "assistant_response": response,
                    "provider": "",
                    "model": "",
                    "calendar_create_draft": draft_json,
                    "messages": [
                        {"role": "user", "content": state["user_message"]},
                        {"role": "assistant", "content": response},
                    ],
                }

            if not draft.calendar_id and draft.calendar_name:
                try:
                    user_uuid = UUID(str(user_id))
                    connection_uuid = UUID(str(connection_id))

                    async def list_all_calendars() -> list[dict[str, Any]]:
                        calendars: list[dict[str, Any]] = []
                        page_token = None
                        seen_tokens: set[str] = set()
                        with SessionLocal() as db:
                            service = CalendarService(db)
                            for _ in range(CALENDAR_LIST_MAX_PAGES):
                                page = await service.list_calendars(
                                    user_id=user_uuid,
                                    connection_id=connection_uuid,
                                    max_results=250,
                                    page_token=page_token,
                                )
                                calendars.extend(page.get("calendars", []))
                                next_token = page.get("next_page_token")
                                if not next_token:
                                    break
                                if next_token in seen_tokens:
                                    raise ValueError(
                                        "Calendar list pagination repeated a token."
                                    )
                                seen_tokens.add(next_token)
                                page_token = next_token
                        return calendars

                    listed_calendars = asyncio.run(list_all_calendars())
                except Exception as exc:
                    logger.warning(
                        "Calendar name resolution failed | error_type=%s",
                        type(exc).__name__,
                    )
                    response = (
                        "I couldn’t resolve the requested Calendar. Please "
                        "choose an accessible calendar from Calendar List."
                    )
                    return {
                        "assistant_response": response,
                        "provider": "",
                        "model": "",
                        "calendar_create_draft": draft_json,
                        "messages": [
                            {"role": "user", "content": state["user_message"]},
                            {"role": "assistant", "content": response},
                        ],
                    }

                matching_calendars = [
                    calendar
                    for calendar in listed_calendars
                    if isinstance(calendar, dict)
                    and isinstance(calendar.get("summary"), str)
                    and calendar["summary"].strip().casefold()
                    == draft.calendar_name.casefold()
                ]
                if len(matching_calendars) != 1:
                    response = (
                        f"I couldn’t uniquely resolve the calendar "
                        f"{draft.calendar_name!r}. Please provide a calendar "
                        "name that matches exactly one accessible calendar."
                    )
                    return {
                        "assistant_response": response,
                        "provider": "",
                        "model": "",
                        "calendar_create_draft": draft_json,
                        "messages": [
                            {"role": "user", "content": state["user_message"]},
                            {"role": "assistant", "content": response},
                        ],
                    }
                draft_json["calendar_id"] = matching_calendars[0]["id"]

            if not draft_json.get("calendar_id"):
                draft_json["calendar_id"] = "primary"
            arguments = draft_to_event_arguments(draft_json)
            if arguments is None:
                response = _calendar_create_missing_details_prompt(
                    ["start_time", "end_time"]
                )
                return {
                    "assistant_response": response,
                    "provider": "",
                    "model": "",
                    "calendar_create_draft": draft_json,
                    "messages": [
                        {"role": "user", "content": state["user_message"]},
                        {"role": "assistant", "content": response},
                    ],
                }
        if not isinstance(arguments, dict):
            message = state["user_message"]
            times = re.findall(
                r"\b\d{1,2}:\d{2}\b|"
                r"\b\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)\b",
                message,
                re.IGNORECASE,
            )
            has_date = re.search(
                r"\b(?:today|tomorrow|monday|tuesday|wednesday|thursday|"
                r"friday|saturday|sunday|20\d{2}-\d{2}-\d{2})\b",
                message,
                re.IGNORECASE,
            )
            if len(times) == 2 and not has_date:
                response = (
                    f"What date should I schedule this event for, "
                    f"{times[0]}–{times[1]}? I’ll use "
                    f"{state.get('user_timezone', 'UTC')}."
                )
            elif len(times) == 1 and has_date:
                response = (
                    f"What end time should I use for this event? "
                    f"I’ll use {state.get('user_timezone', 'UTC')}."
                )
            elif len(times) == 1:
                response = (
                    f"What date and end time should I use for this event? "
                    f"I’ll use {state.get('user_timezone', 'UTC')}."
                )
            elif not times:
                response = (
                    "What date and start and end times should I use for the "
                    "Calendar event? I’ll use your selected timezone."
                )
            else:
                response = (
                    "What title and start and end times should I use for the "
                    "Calendar event?"
                )
            return {
                "assistant_response": response,
                "provider": "",
                "model": "",
                "messages": [
                    {"role": "user", "content": state["user_message"]},
                    {"role": "assistant", "content": response},
                ],
            }
        try:
            parameters = CalendarCreateEventParameters.model_validate(arguments)
        except ValidationError:
            response = (
                "I couldn’t validate the event details. Please provide a title, "
                "calendar, start and end, and an explicit timezone for timed "
                "events. Nothing was created."
            )
            return {
                "assistant_response": response,
                "provider": "",
                "model": "",
                "messages": [
                    {"role": "user", "content": state["user_message"]},
                    {"role": "assistant", "content": response},
                ],
            }
        except PermissionError:
            response = (
                "The selected Google account is missing Calendar write "
                "permission. Reconnect or grant Calendar event access; nothing "
                "was created."
            )
            return {
                "assistant_response": response,
                "provider": "",
                "model": "",
                "messages": [
                    {"role": "user", "content": state["user_message"]},
                    {"role": "assistant", "content": response},
                ],
            }
        except Exception as exc:
            logger.exception(
                "Calendar create proposal preflight failed | user_id=%s "
                "| error_type=%s",
                user_id,
                type(exc).__name__,
            )
            response = (
                "I couldn’t verify access to that Calendar. Check the calendar "
                "ID and the selected account’s access, then try again. Nothing "
                "was created."
            )
            return {
                "assistant_response": response,
                "provider": "",
                "model": "",
                "messages": [
                    {"role": "user", "content": state["user_message"]},
                    {"role": "assistant", "content": response},
                ],
            }

        serialized = parameters.model_dump(mode="json")
        if serialized.get("reminders") is None:
            serialized.pop("reminders")
        if serialized.get("recurrence") is None:
            serialized.pop("recurrence")
        if parameters.all_day:
            event_time = f"{parameters.start} (all day)"
        else:
            event_time = (
                f"{parameters.start} to {parameters.end} "
                f"({parameters.time_zone})"
            )
        response = (
            f"I prepared an approval request to create **{parameters.summary}** "
            f"on calendar `{parameters.calendar_id}` for {event_time}. "
            "Review the approval card below. Google Calendar will not be changed "
            "unless you approve and separately execute the request."
        )
        return {
            "assistant_response": response,
            "provider": "",
            "model": "",
            "calendar_create_event_proposal": serialized,
            "calendar_create_draft": draft_json
            if isinstance(draft_data, dict)
            else None,
            "messages": [
                {"role": "user", "content": state["user_message"]},
                {"role": "assistant", "content": response},
            ],
        }

    if tool_name == "create_calendar_event_proposal":
        proposal_result = _create_event_proposal(state, runtime, arguments)
        supersedes_proposal_id = plan.get("supersedes_proposal_id")
        if (
            proposal_result.get("calendar_create_event_proposal") is not None
            and isinstance(supersedes_proposal_id, str)
        ):
            proposal_result["calendar_proposal_supersedes_id"] = (
                supersedes_proposal_id
            )
        return proposal_result

    if tool_name == "create_event_after_availability_check":
        combined_arguments = (
            arguments if isinstance(arguments, dict) else {}
        )
        parameters_payload = combined_arguments.get("parameters")
        context = runtime.context
        user_id = context.get("user_id")
        event_connection_id = context.get("calendar_event_connection_id")
        time_zone = (
            combined_arguments.get("time_zone")
            or state.get("user_timezone")
            or "UTC"
        )
        calendar_id = combined_arguments.get("calendar_id") or "primary"
        time_min = combined_arguments.get("time_min")
        time_max = combined_arguments.get("time_max")
        if not user_id or not event_connection_id:
            response = (
                "I couldn’t check your Google Calendar because event-read "
                "access is unavailable. Nothing was created."
            )
            return {
                "assistant_response": response,
                "provider": "",
                "model": "",
                "messages": [
                    {"role": "user", "content": state["user_message"]},
                    {"role": "assistant", "content": response},
                ],
            }
        if not isinstance(parameters_payload, dict):
            response = (
                "I couldn’t safely validate the event details for that "
                "Calendar request. Nothing was created."
            )
            return {
                "assistant_response": response,
                "provider": "",
                "model": "",
                "messages": [
                    {"role": "user", "content": state["user_message"]},
                    {"role": "assistant", "content": response},
                ],
            }
        try:
            calendar_parameters = CalendarCreateEventParameters.model_validate(
                parameters_payload
            )
        except ValidationError:
            response = (
                "I couldn’t safely validate the event details for that "
                "Calendar request. Nothing was created."
            )
            return {
                "assistant_response": response,
                "provider": "",
                "model": "",
                "messages": [
                    {"role": "user", "content": state["user_message"]},
                    {"role": "assistant", "content": response},
                ],
            }
        try:
            tools = create_calendar_tools(
                user_id=user_id,
                connection_id=event_connection_id,
                freebusy_connection_id=(
                    context.get("calendar_freebusy_connection_id")
                ),
                include_event_tools=True,
                include_freebusy_tool=bool(
                    context.get("calendar_freebusy_connection_id")
                ),
                request_time=context.get("request_time"),
            )
            list_tool = next(
                (tool for tool in tools if tool.name == "list_calendar_events"),
                None,
            )
            if list_tool is None:
                raise LookupError("Calendar event listing is unavailable.")
            tool_output = asyncio.run(list_tool.ainvoke({
                "time_min": time_min,
                "time_max": time_max,
                "time_zone": time_zone,
                "calendar_id": calendar_id,
                "max_results": 100,
            }))
            envelope = json.loads(tool_output)
        except Exception as exc:
            logger.warning(
                "Calendar day availability check failed | user_id=%s | "
                "error_type=%s",
                user_id,
                type(exc).__name__,
            )
            response = (
                "I couldn’t check your Google Calendar for that time, so I "
                "did not prepare an event. Nothing was created."
            )
            return {
                "assistant_response": response,
                "provider": "",
                "model": "",
                "messages": [
                    {"role": "user", "content": state["user_message"]},
                    {"role": "assistant", "content": response},
                ],
            }
        if not envelope.get("ok"):
            error = envelope.get("error")
            response = (
                error.get("message")
                if isinstance(error, dict)
                and isinstance(error.get("message"), str)
                else (
                    "I couldn’t check your Google Calendar for that time, so "
                    "I did not prepare an event. Nothing was created."
                )
            )
            return {
                "assistant_response": response,
                "provider": "",
                "model": "",
                "messages": [
                    {"role": "user", "content": state["user_message"]},
                    {"role": "assistant", "content": response},
                ],
            }
        data = envelope.get("data")
        events = data.get("events") if isinstance(data, dict) else None
        if not isinstance(events, list):
            response = (
                "Google Calendar returned an incomplete day view, so I did "
                "not prepare an event. Nothing was created."
            )
            return {
                "assistant_response": response,
                "provider": "",
                "model": "",
                "messages": [
                    {"role": "user", "content": state["user_message"]},
                    {"role": "assistant", "content": response},
                ],
            }
        requested_start = datetime.fromisoformat(
            calendar_parameters.start.replace("Z", "+00:00")
        )
        requested_end = datetime.fromisoformat(
            calendar_parameters.end.replace("Z", "+00:00")
        )
        conflicts = _calendar_conflict_lines(
            events,
            requested_start,
            requested_end,
            time_zone,
        )
        if conflicts:
            conflict_text = "; ".join(conflicts[:5])
            response = (
                "I checked your Google Calendar for that time and found a "
                f"conflict: {conflict_text}. I did not create the event. "
                "Choose another time and I’ll check again."
            )
            return {
                "assistant_response": response,
                "provider": "",
                "model": "",
                "messages": [
                    {"role": "user", "content": state["user_message"]},
                    {"role": "assistant", "content": response},
                ],
            }
        proposal_result = _create_event_proposal(
            state,
            runtime,
            calendar_parameters.model_dump(mode="json"),
        )
        if proposal_result.get("calendar_create_event_proposal") is not None:
            proposal_result["calendar_availability_checked"] = True
        return proposal_result

    context = runtime.context
    user_id = context.get("user_id")
    event_connection_id = context.get("calendar_event_connection_id")
    freebusy_connection_id = context.get("calendar_freebusy_connection_id")
    if not user_id:
        response = "Calendar access is unavailable for this request."
        return {
            "assistant_response": response,
            "provider": "",
            "model": "",
            "messages": [
                {"role": "user", "content": state["user_message"]},
                {"role": "assistant", "content": response},
            ],
        }

    is_freebusy = tool_name == "check_calendar_availability"
    selected_connection_id = (
        freebusy_connection_id if is_freebusy else event_connection_id
    )
    if not selected_connection_id:
        if context.get("calendar_default_connection_invalid", False):
            response = (
                "Your saved default Google Calendar account is unavailable. "
                "Choose a connected account in Connected Apps, then try again."
            )
        elif (
            context.get(
                "calendar_freebusy_selection_required"
                if is_freebusy
                else "calendar_event_selection_required",
                False,
            )
        ):
            response = (
                "More than one connected Google account can perform this "
                "Calendar operation. Choose your default Calendar account "
                "in Connected Apps, then try again."
            )
        elif context.get(
            "calendar_selected_freebusy_scope_missing"
            if is_freebusy
            else "calendar_selected_event_scope_missing",
            False,
        ):
            permission = "free/busy" if is_freebusy else "event-read"
            response = (
                f"Your selected Google account lacks the Calendar "
                f"{permission} permission. Re-consent for that account in "
                "Connected Apps. I will not switch to another account."
            )
        elif not context.get("google_connection_available", False):
            response = (
                "Google Calendar is not connected. Connect a Google account "
                "with Calendar access in Connected Apps, then try again."
            )
        elif is_freebusy and event_connection_id:
            response = (
                "Your connected account can read Calendar events, but it "
                "doesn’t have the separate permission required to check "
                "free/busy availability."
            )
        elif is_freebusy:
            response = (
                "Your connected Google account needs the Calendar free/busy "
                "permission before I can check availability."
            )
        else:
            response = (
                "Your connected Google account needs Calendar event-read "
                "permission before I can read events."
            )
        return {
            "assistant_response": response,
            "provider": "",
            "model": "",
            "messages": [
                {"role": "user", "content": state["user_message"]},
                {"role": "assistant", "content": response},
            ],
        }

    try:
        tool_context = {
            "user_id": user_id,
            "connection_id": event_connection_id or freebusy_connection_id,
            "freebusy_connection_id": freebusy_connection_id,
            "include_event_tools": bool(event_connection_id),
            "include_freebusy_tool": bool(freebusy_connection_id),
        }
        request_time = context.get("request_time")
        if request_time is not None:
            tool_context["request_time"] = request_time
        tools = create_calendar_tools(**tool_context)
        selected_tool = next(
            (tool for tool in tools if tool.name == tool_name),
            None,
        )
        arguments = dict(arguments) if isinstance(arguments, dict) else {}
        response: str | None = None
        requested_calendar = _requested_calendar_name(state["user_message"])
        if selected_tool is not None and requested_calendar is not None:
            calendar_list_tool = next(
                (tool for tool in tools if tool.name == "list_calendars"),
                None,
            )
            if calendar_list_tool is None:
                response = (
                    "Calendar List access is required to find the requested "
                    "calendar. Grant Calendar List Access in Connected Apps."
                )
                selected_tool = None
            else:
                calendar_list_output = asyncio.run(
                    _list_all_calendars(calendar_list_tool)
                )
                calendar_list_envelope = json.loads(calendar_list_output)
                if not calendar_list_envelope.get("ok"):
                    error = calendar_list_envelope.get("error")
                    response = (
                        error.get("message")
                        if isinstance(error, dict)
                        and isinstance(error.get("message"), str)
                        else "Google Calendar could not list accessible calendars."
                    )
                    selected_tool = None
                else:
                    calendar_list_data = calendar_list_envelope.get("data")
                    calendars = (
                        calendar_list_data.get("calendars")
                        if isinstance(calendar_list_data, dict)
                        else None
                    )
                    matches = (
                        _matching_calendars(calendars, requested_calendar)
                        if isinstance(calendars, list)
                        else []
                    )
                    if len(matches) != 1:
                        names = [
                            calendar["summary"].strip()
                            for calendar in matches
                        ]
                        if names:
                            response = (
                                f"More than one calendar matches "
                                f"“{requested_calendar}”: "
                                f"{', '.join(names)}. Which one do you mean?"
                            )
                        else:
                            response = (
                                f"I couldn’t find an accessible calendar "
                                f"named “{requested_calendar}”. Please choose "
                                "one from the calendars available to your "
                                "connected Google account."
                            )
                        selected_tool = None
                    else:
                        arguments["calendar_id"] = matches[0]["id"]
        if selected_tool is None:
            if response is None:
                response = (
                    "Free/Busy is unavailable because the selected Google "
                    "account has not granted the optional Calendar free/busy "
                    "permission."
                    if is_freebusy
                    else (
                        "That Calendar operation is not available with the "
                        "permissions currently granted to your account."
                    )
                )
        else:
            collect_all_event_pages = (
                tool_name == "list_calendar_events"
                and re.search(
                    r"\b(?:all pages|every page|all matching events)\b",
                    state["user_message"],
                    re.IGNORECASE,
                )
                is not None
            )
            if tool_name == "list_calendars":
                tool_output = asyncio.run(_list_all_calendars(selected_tool))
            elif collect_all_event_pages:
                tool_output = asyncio.run(
                    _list_all_calendar_events(selected_tool, arguments)
                )
            else:
                tool_output = asyncio.run(selected_tool.ainvoke(arguments))
            envelope = json.loads(tool_output)
            if not envelope.get("ok"):
                error = envelope.get("error")
                response = (
                    error.get("message")
                    if isinstance(error, dict)
                    and isinstance(error.get("message"), str)
                    else "Google Calendar could not complete that read request."
                )
            else:
                data = envelope.get("data")
                if not isinstance(data, dict):
                    response = (
                        "Google Calendar returned an incomplete result, so "
                        "I can’t provide a reliable answer."
                    )
                else:
                    title_substring = _requested_title_substring(
                        state["user_message"],
                    )
                    if title_substring is not None:
                        matched_events = data.get("events")
                        if isinstance(matched_events, list):
                            data = {
                                **data,
                                "events": [
                                    event
                                    for event in matched_events
                                    if isinstance(event, dict)
                                    and isinstance(event.get("summary"), str)
                                    and title_substring.casefold()
                                    in event["summary"].casefold()
                                ],
                            }
                    timezone = state.get("user_timezone", "UTC")
                    requested_calendar_ids = arguments.get("calendar_ids")
                    if next_event_details:
                        events = data.get("events")
                        if not isinstance(events, list) or any(
                            not isinstance(event, dict)
                            or not isinstance(event.get("id"), str)
                            or not event["id"].strip()
                            for event in events
                        ):
                            response = _render_result(
                                tool_name,
                                data,
                                timezone,
                            )
                        else:
                            selected_event = _select_next_event(
                                events,
                                next_event_request_time,
                                next_event_timezone,
                            )
                            if selected_event is None:
                                response = (
                                    "Google Calendar returned malformed event "
                                    "times, so I couldn’t identify the next "
                                    "upcoming event."
                                )
                            elif not selected_event:
                                response = (
                                    "There are no upcoming events on your "
                                    "Google Calendar."
                                )
                            else:
                                response = _render_result(
                                    tool_name,
                                    {
                                        **data,
                                        "events": [selected_event],
                                        "next_page_token": None,
                                    },
                                    next_event_timezone,
                                )
                    else:
                        response = _render_result(
                            tool_name,
                            data,
                            timezone,
                            requested_calendar_ids=(
                                requested_calendar_ids
                                if isinstance(requested_calendar_ids, list)
                                and all(
                                    isinstance(item, str)
                                    for item in requested_calendar_ids
                                )
                                else None
                            ),
                        )
    except (ValidationError, ValueError, json.JSONDecodeError):
        response = (
            "I couldn’t validate that Calendar request. Please clarify the "
            "date, time range, or event details."
        )
    calendar_events: list[dict[str, Any]] = []
    try:
        if (
            tool_name in {
                "list_calendar_events",
                "get_calendar_event",
                "get_today_schedule",
                "get_calendar_schedule",
            }
            and "data" in locals()
            and isinstance(locals()["data"], dict)
        ):
            calendar_events = _structured_calendar_events(
                locals()["data"],
                state.get("user_timezone", "UTC"),
                arguments.get("calendar_id") if isinstance(arguments, dict) else None,
            )
    except Exception:
        calendar_events = []

    result: dict[str, Any] = {
        "assistant_response": response,
        "provider": "",
        "model": "",
        "calendar_events": calendar_events,
        "messages": [
            {"role": "user", "content": state["user_message"]},
            {"role": "assistant", "content": response},
        ],
    }
    # Persist scoped Calendar read context for legitimate date/refinement follow-ups.
    try:
        if tool_name in {
            "list_calendar_events",
            "get_calendar_event",
            "get_today_schedule",
            "get_calendar_schedule",
            "check_calendar_availability",
            "list_calendars",
        }:
            req_time = state.get("request_time")
            now_dt = datetime.fromisoformat(str(req_time).replace("Z", "+00:00")) if req_time else datetime.now(timezone.utc)
            pending_read = {
                "version": 1,
                "type": "calendar_read_continuation",
                "user_id": str(state.get("user_id")),
                "conversation_id": str(state.get("conversation_id")),
                "expires_at": (now_dt + timedelta(minutes=10)).isoformat(),
                "calendar_id": (arguments.get("calendar_id") if isinstance(arguments, dict) and isinstance(arguments.get("calendar_id"), str) else "primary"),
                "time_zone": state.get("user_timezone", "UTC"),
                "last_tool": tool_name,
                "last_request": state.get("user_message"),
            }
            if isinstance(arguments, dict) and isinstance(arguments.get("query"), str):
                pending_read["query"] = arguments.get("query")
            result["pending_calendar_read"] = pending_read
    except Exception:
        pass
    return result
