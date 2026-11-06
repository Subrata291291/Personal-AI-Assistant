import asyncio
import json
import logging
import re
from datetime import date, datetime, time, timezone
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
from app.workflows.calendar_tools import create_calendar_tools
from app.workflows.chat_state import ChatContext, ChatState

logger = logging.getLogger(__name__)


def _is_next_event_details_request(message: str) -> bool:
    return bool(re.search(
        r"\b(?:show|get|find|tell me|what is|what's)\b.{0,50}"
        r"\bdetails?\b.{0,40}\b(?:next|upcoming)\s+(?:meeting|event)\b|"
        r"\bdetails?\b.{0,40}\b(?:next|upcoming)\s+(?:meeting|event)\b",
        message.casefold(),
    ))


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
        lines.append(f"  - Details: {description.strip()[:2000]}")
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
    feature_key = f"{operation}_feature_enabled"
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
    if not capabilities.get(
        feature_key,
        capabilities.get(enabled_key, False),
    ):
        response = (
            "I can read your Google Calendar, but Calendar changes are "
            "currently unavailable. I haven't created or modified anything."
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
    connection_id = context.get("calendar_write_connection_id")
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
    elif not capabilities.get(enabled_key, False):
        response = (
            "The selected Google account does not have the enabled Calendar "
            "write capability. Nothing was changed."
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
        if normalized_query not in " ".join(latest_message.split()):
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
                event_timezone = state.get("user_timezone", "UTC")
                if "start" in changes:
                    for endpoint in ("start", "end"):
                        event_time = changes.get(endpoint)
                        if isinstance(event_time, dict) and "date" not in event_time:
                            event_time.setdefault("timeZone", event_timezone)
                    time_zone = event_timezone
                else:
                    time_zone = None
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
        response = (
            f"I prepared an approval request to {verb} **{event_title}**. "
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
        create_enabled = capabilities.get(
            "create_feature_enabled",
            capabilities.get(
                "create_execution_available",
                capabilities.get("write_execution_available", False),
            ),
        )
        if not create_enabled:
            response = (
                "I can read your Google Calendar, but creating events is "
                "currently unavailable. I haven't created or modified anything."
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
        if context.get("calendar_selected_write_scope_missing", False):
            response = (
                "Your selected Google account is missing Calendar write "
                "permission. Grant access for that account in Connected Apps. "
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
        if not capabilities.get(
            "create_execution_available",
            capabilities.get("write_execution_available", False),
        ):
            response = (
                "The selected Google account does not have the enabled "
                "Calendar write capability. Nothing was created."
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
        if not connection_id or not user_id or not isinstance(arguments, dict):
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

        try:
            parameters = CalendarCreateEventParameters.model_validate(arguments)
            with SessionLocal() as db:
                asyncio.run(
                    CalendarService(db).validate_create_event_access(
                        user_id=UUID(str(user_id)),
                        connection_id=UUID(str(connection_id)),
                        calendar_id=parameters.calendar_id,
                    )
                )
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
            "messages": [
                {"role": "user", "content": state["user_message"]},
                {"role": "assistant", "content": response},
            ],
        }

    if tool_name == "create_calendar_event_proposal":
        return _create_event_proposal(state, runtime, arguments)

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
        if selected_tool is None:
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
    return {
        "assistant_response": response,
        "provider": "",
        "model": "",
        "messages": [
            {"role": "user", "content": state["user_message"]},
            {"role": "assistant", "content": response},
        ],
    }
