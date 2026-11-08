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
from app.workflows.calendar_tools import create_calendar_tools
from app.workflows.chat_state import ChatContext, ChatState
from app.workflows.calendar_create_draft import (
    CalendarCreateDraft,
    draft_missing_fields,
    draft_to_event_arguments,
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


def _is_next_event_details_request(message: str) -> bool:
    return bool(re.search(
        r"\b(?:show|get|find|tell me|what is|what's)\b.{0,50}"
        r"\bdetails?\b.{0,40}\b(?:next|upcoming)\s+(?:meeting|event)\b|"
        r"\bdetails?\b.{0,40}\b(?:next|upcoming)\s+(?:meeting|event)\b",
        message.casefold(),
    ))


def _requested_calendar_name(message: str) -> str | None:
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
    if not name or name.casefold() in {"google", "primary", "my", "the"}:
        return None
    return name


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
        return (
            "What date and time range should I check? Please include the "
            "start and end time; I’ll use your selected timezone."
        )
    if re.search(r"\b(?:overlapping|conflicts?)\b", lowered):
        return (
            "Which date or date range should I check for overlapping "
            "Calendar events?"
        )
    if re.search(r"\b(?:create|add|book|schedule|cancel|delete|remove|"
                 r"update|edit|change|modify|move|reschedule|invite|rsvp)\b",
                 lowered):
        return (
            "I can read your Google Calendar, but Calendar changes are not "
            "available. I have not created or modified anything."
        )
    return (
        "I couldn’t determine which Calendar information you want. Please "
        "specify a date range, event title, or event ID."
    )


def _format_event_time(
    event_time: dict[str, Any] | None,
    timezone: str,
) -> str:
    if not isinstance(event_time, dict):
        return "time unavailable"
    all_day = event_time.get("date")
    if isinstance(all_day, str):
        return f"{all_day} (all day)"
    raw = event_time.get("date_time")
    if not isinstance(raw, str):
        return "time unavailable"
    try:
        parsed = datetime.fromisoformat(
            raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        )
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return "time unavailable"
        local = parsed.astimezone(ZoneInfo(timezone))
    except (ValueError, KeyError):
        return "time unavailable"
    return local.strftime("%Y-%m-%d %I:%M %p %Z")


def _format_local_datetime(value: Any, timezone: str) -> str:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(
                value[:-1] + "+00:00" if value.endswith("Z") else value
            )
        except ValueError:
            return "time unavailable"
    else:
        return "time unavailable"
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return "time unavailable"
    try:
        local = parsed.astimezone(ZoneInfo(timezone))
    except (ValueError, KeyError):
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


def _format_event(event: dict[str, Any], timezone: str) -> str:
    summary = event.get("summary")
    title = summary.strip() if isinstance(summary, str) and summary.strip() else "Untitled event"
    start = _format_event_time(event.get("start"), timezone)
    end = _format_event_time(event.get("end"), timezone)
    lines = [f"- **{title}** — {start} to {end}"]
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
        _format_event(event, timezone)
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
    return {
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


def _resolve_relative_time_changes(
    event: dict[str, Any],
    changes: dict[str, Any],
    fallback_timezone: str,
) -> tuple[dict[str, Any], str | None]:
    requested_start_time = changes.pop("start_time", None)
    duration_delta_minutes = changes.pop("duration_delta_minutes", None)
    if requested_start_time is None and duration_delta_minutes is None:
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
    if requested_start_time is not None:
        if not isinstance(requested_start_time, str) or not re.fullmatch(
            r"(?:[01]\d|2[0-3]):[0-5]\d",
            requested_start_time,
        ):
            raise ValueError("A local 24-hour HH:MM start time is required.")
        hour, minute = (int(value) for value in requested_start_time.split(":"))
        local_current_start = event_start.astimezone(zone)
        naive_start = datetime.combine(
            local_current_start.date(),
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


def _prepare_event_mutation(
    state: ChatState,
    runtime: Runtime[ChatContext],
    arguments: object,
    action_type: str,
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
            "I couldn’t identify the Calendar event or requested changes. "
            "Please specify the event and what should change."
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

    calendar_id = arguments.get("calendar_id", "primary")
    event_id = arguments.get("event_id")
    event_query = arguments.get("event_query")
    changes = arguments.get("changes")
    latest_message = state["user_message"].casefold()
    if isinstance(event_id, str) and event_id.casefold() not in latest_message:
        event_id = None
    if isinstance(event_query, str):
        normalized_query = " ".join(event_query.casefold().split())
        recent_messages = state.get("messages", [])[-6:]
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
        ):
            event_query = None
    if event_id is None and event_query is None:
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
            if not isinstance(event_id, str) or not event_id.strip():
                if not isinstance(event_query, str) or not event_query.strip():
                    raise ValueError("An event ID or event search is required.")
                matches = asyncio.run(
                    service.list_events(
                        user_id=UUID(str(user_id)),
                        connection_id=UUID(str(connection_id)),
                        calendar_id=calendar_id,
                        max_results=10,
                        query=event_query,
                        time_zone=state.get("user_timezone", "UTC"),
                    )
                )
                events = matches.get("events")
                if not isinstance(events, list):
                    raise ValueError("Calendar returned invalid event results.")
                if not events:
                    response = (
                        "Google Calendar returned no matching events for this "
                        "live request. Nothing was changed."
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
                if len(events) != 1 or matches.get("next_page_token"):
                    choices = "\n".join(
                        _format_event(event, state.get("user_timezone", "UTC"))
                        for event in events[:5]
                        if isinstance(event, dict)
                    )
                    response = (
                        "I found multiple possible Calendar events. Which one "
                        "do you mean? Please identify it by title and time:\n\n"
                        f"{choices}"
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
                selected = events[0]
                event_id = selected.get("id") if isinstance(selected, dict) else None
                if not isinstance(event_id, str) or not event_id:
                    raise ValueError("Calendar returned an invalid event.")

            event = asyncio.run(
                service.validate_event_write_access(
                    user_id=UUID(str(user_id)),
                    connection_id=UUID(str(connection_id)),
                    calendar_id=calendar_id,
                    event_id=event_id,
                )
            )
            if event.get("recurrence") and not event.get("recurring_event_id"):
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
                event_timezone = state.get("user_timezone", "UTC")
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
                })
    except ValidationError:
        response = (
            "I couldn’t validate the requested Calendar change. Please clarify "
            "the event and the exact change. Nothing was changed."
        )
    except ValueError:
        response = (
            "I couldn’t safely apply the requested time change to this event. "
            "Please clarify the time and timezone. Nothing was changed."
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


def calendar_agent_node(
    state: ChatState,
    runtime: Runtime[ChatContext],
) -> dict[str, Any]:
    plan = state.get("gmail_plan") or {}
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
    tool_name = plan.get("calendar_tool")
    arguments = plan.get("calendar_arguments")
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
        )
    if tool_name == "clarify" or (
        not isinstance(arguments, dict)
        and tool_name != "create_calendar_event_proposal"
    ):
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
                response = (
                    "I can read your Google Calendar, but Calendar changes "
                    "are not available. I have not created or modified "
                    "anything."
                )
        else:
            response = _clarification(state)
        return {
            "assistant_response": response,
            "provider": "",
            "model": "",
            "messages": [
                {"role": "user", "content": state["user_message"]},
                {"role": "assistant", "content": response},
            ],
        }


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
            response = (
                "I couldn’t safely select an authorized Google Calendar account "
                "for this event. Nothing was created."
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
            tool_output = asyncio.run(
                _list_all_calendars(selected_tool)
                if tool_name == "list_calendars"
                else selected_tool.ainvoke(arguments)
            )
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
    return {
        "assistant_response": response,
        "provider": "",
        "model": "",
        "calendar_events": calendar_events,
        "messages": [
            {"role": "user", "content": state["user_message"]},
            {"role": "assistant", "content": response},
        ],
    }
