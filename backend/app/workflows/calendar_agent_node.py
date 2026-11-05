import asyncio
import json
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from langgraph.runtime import Runtime
from pydantic import ValidationError

from app.workflows.calendar_tools import create_calendar_tools
from app.workflows.chat_state import ChatContext, ChatState


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
                 r"update|edit|change|modify|invite|rsvp)\b", lowered):
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


def _format_event(event: dict[str, Any], timezone: str) -> str:
    summary = event.get("summary")
    title = summary.strip() if isinstance(summary, str) and summary.strip() else "Untitled event"
    start = _format_event_time(event.get("start"), timezone)
    end = _format_event_time(event.get("end"), timezone)
    lines = [f"- **{title}** — {start} to {end}"]
    location = event.get("location")
    if isinstance(location, str) and location.strip():
        lines.append(f"  - Location: {location.strip()}")
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
            return (
                f"{scope} for the requested interval "
                f"({data.get('time_min')} to {data.get('time_max')}, "
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
    if not isinstance(events, list):
        events = [data] if isinstance(data.get("id"), str) else []
    if not events:
        return "I didn’t find any matching Calendar events."
    result = f"I found {len(events)} Calendar event(s):\n\n"
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


def calendar_agent_node(
    state: ChatState,
    runtime: Runtime[ChatContext],
) -> dict[str, Any]:
    plan = state.get("gmail_plan") or {}
    tool_name = plan.get("calendar_tool")
    arguments = plan.get("calendar_arguments")
    if tool_name == "clarify" or not isinstance(arguments, dict):
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
        tools = create_calendar_tools(
            user_id=user_id,
            connection_id=event_connection_id or freebusy_connection_id,
            freebusy_connection_id=freebusy_connection_id,
            include_event_tools=bool(event_connection_id),
            include_freebusy_tool=bool(freebusy_connection_id),
        )
        selected_tool = next(
            (tool for tool in tools if tool.name == tool_name),
            None,
        )
        if selected_tool is None:
            response = (
                "That Calendar operation is not available with the "
                "permissions currently granted to your account."
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
                    timezone = state.get("user_timezone", "UTC")
                    requested_calendar_ids = arguments.get("calendar_ids")
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
    return {
        "assistant_response": response,
        "provider": "",
        "model": "",
        "messages": [
            {"role": "user", "content": state["user_message"]},
            {"role": "assistant", "content": response},
        ],
    }
