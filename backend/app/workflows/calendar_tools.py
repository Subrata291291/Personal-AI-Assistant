from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date as CalendarDate
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi.encoders import jsonable_encoder
from langchain_core.tools import StructuredTool
from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.integrations.google.exceptions import GoogleAPIError
from app.services.calendar_service import CalendarService


class _CalendarToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ListCalendarEventsInput(_CalendarToolInput):
    calendar_id: str = Field(default="primary", min_length=1, max_length=1024)
    time_min: datetime | None = None
    time_max: datetime | None = None
    query: str | None = Field(default=None, max_length=500)
    attendee: EmailStr | None = None
    time_zone: str | None = Field(default=None, min_length=1, max_length=100)
    max_results: int = Field(default=100, ge=1, le=2500)
    page_token: str | None = Field(default=None, min_length=1, max_length=4096)

    @field_validator("time_min", "time_max")
    @classmethod
    def require_aware_datetimes(
        cls,
        value: datetime | None,
    ) -> datetime | None:
        if value is not None and (
            value.tzinfo is None or value.utcoffset() is None
        ):
            raise ValueError("Datetime values must include a UTC offset.")
        return value

    @model_validator(mode="after")
    def require_ordered_range(self) -> ListCalendarEventsInput:
        if (
            self.time_min is not None
            and self.time_max is not None
            and self.time_max <= self.time_min
        ):
            raise ValueError("time_max must be later than time_min.")
        return self


class GetCalendarEventInput(_CalendarToolInput):
    event_id: str = Field(min_length=1, max_length=1024)
    calendar_id: str = Field(default="primary", min_length=1, max_length=1024)


class ListCalendarsInput(_CalendarToolInput):
    max_results: int = Field(default=100, ge=1, le=250)
    page_token: str | None = Field(default=None, min_length=1, max_length=4096)


class GetTodayScheduleInput(_CalendarToolInput):
    time_zone: str = Field(min_length=1, max_length=100)
    calendar_id: str = Field(default="primary", min_length=1, max_length=1024)


class GetCalendarScheduleInput(_CalendarToolInput):
    schedule_date: CalendarDate = Field(alias="date")
    period: Literal["day", "week", "month"]
    time_zone: str = Field(min_length=1, max_length=100)
    calendar_id: str = Field(default="primary", min_length=1, max_length=1024)


class CheckCalendarAvailabilityInput(_CalendarToolInput):
    time_min: datetime
    time_max: datetime
    time_zone: str = Field(min_length=1, max_length=100)
    calendar_ids: list[str] = Field(
        default_factory=lambda: ["primary"],
        min_length=1,
        max_length=50,
    )

    @field_validator("time_min", "time_max")
    @classmethod
    def require_aware_datetimes(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Datetime values must include a UTC offset.")
        return value

    @model_validator(mode="after")
    def require_ordered_range(self) -> CheckCalendarAvailabilityInput:
        if self.time_max <= self.time_min:
            raise ValueError("time_max must be later than time_min.")
        return self


def _tool_error(exc: Exception) -> dict[str, str]:
    if isinstance(exc, PermissionError):
        return {
            "code": "calendar_permission_missing",
            "message": "The selected Google account lacks permission for this Calendar operation.",
        }
    if isinstance(exc, ValueError):
        if str(exc) == "Connected Google account was not found.":
            return {
                "code": "calendar_connection_missing",
                "message": "The selected Google Calendar connection is unavailable.",
            }
        if str(exc) == "Google OAuth credentials were not found.":
            return {
                "code": "calendar_credentials_missing",
                "message": "The selected Google Calendar credentials are unavailable.",
            }
        return {
            "code": "invalid_calendar_request",
            "message": "The Calendar request contains invalid values.",
        }
    if isinstance(exc, GoogleAPIError):
        if exc.status_code == 401:
            return {
                "code": "calendar_authorization_expired",
                "message": "Google Calendar authorization is no longer valid. Reconnect the selected account in Connected Apps and try again.",
            }
        if exc.status_code == 403:
            if exc.reason == "insufficientPermissions":
                return {
                    "code": "calendar_permission_denied",
                    "message": (
                        "Google denied this Calendar permission. Check the "
                        "selected account's Calendar grants in Connected Apps "
                        "and re-consent if needed."
                    ),
                }
            return {
                "code": "calendar_permission_denied",
                "message": "Google denied access to this Calendar resource.",
            }
        if exc.status_code == 404:
            return {
                "code": "calendar_resource_not_found",
                "message": "The requested Calendar resource was not found.",
            }
        if exc.status_code == 429 or exc.retryable or exc.status_code >= 500:
            return {
                "code": "calendar_temporarily_unavailable",
                "message": "Google Calendar is temporarily unavailable. Try again later.",
            }
        return {
            "code": "calendar_provider_error",
            "message": "Google Calendar could not complete the read request.",
        }
    raise exc


def create_calendar_tools(
    *,
    user_id: UUID | str,
    connection_id: UUID | str,
    freebusy_connection_id: UUID | str | None = None,
    request_time: datetime | str | None = None,
    include_event_tools: bool = True,
    include_freebusy_tool: bool = True,
    db_factory: Callable[[], Session] | None = None,
) -> list[StructuredTool]:
    """Create Calendar tools bound to authenticated, server-selected context.

    Identity and connection IDs are captured by these closures and are not
    exposed as LLM-generated tool arguments. CalendarService validates that
    the selected connection is active, owned by this user, and has the scope
    required for each operation.
    """
    authenticated_user_id = UUID(str(user_id))
    selected_connection_id = UUID(str(connection_id))
    selected_freebusy_connection_id = UUID(
        str(freebusy_connection_id or connection_id)
    )
    make_db = db_factory or SessionLocal
    captured_request_time: datetime | None = None
    if isinstance(request_time, datetime):
        captured_request_time = request_time
    elif isinstance(request_time, str):
        try:
            captured_request_time = datetime.fromisoformat(
                request_time[:-1] + "+00:00"
                if request_time.endswith("Z")
                else request_time
            )
        except ValueError as exc:
            raise ValueError("Invalid Calendar request_time.") from exc
    elif request_time is not None:
        raise ValueError("Invalid Calendar request_time.")
    if (
        captured_request_time is not None
        and (
            captured_request_time.tzinfo is None
            or captured_request_time.utcoffset() is None
        )
    ):
        raise ValueError("Calendar request_time must include a UTC offset.")

    async def execute(
        method_name: str,
        arguments: dict[str, Any],
    ) -> str:
        try:
            with make_db() as db:
                service = CalendarService(db)
                method = getattr(service, method_name)
                result = await method(
                    user_id=authenticated_user_id,
                    connection_id=(
                        selected_freebusy_connection_id
                        if method_name == "query_freebusy"
                        else selected_connection_id
                    ),
                    **arguments,
                )
            envelope = {"ok": True, "data": jsonable_encoder(result)}
        except PermissionError as exc:
            if method_name == "list_calendars":
                envelope = {
                    "ok": False,
                    "error": {
                        "code": "calendar_list_permission_missing",
                        "message": (
                            "Calendar List access is required to discover "
                            "calendar names. Grant Calendar List Access for "
                            "this account in Connected Apps, then try again."
                        ),
                    },
                }
            else:
                envelope = {"ok": False, "error": _tool_error(exc)}
        except (ValueError, GoogleAPIError) as exc:
            envelope = {"ok": False, "error": _tool_error(exc)}
        return json.dumps(envelope, ensure_ascii=False, sort_keys=True)

    async def list_calendar_events(**arguments: Any) -> str:
        return await execute("list_events", arguments)

    async def list_calendars(**arguments: Any) -> str:
        return await execute("list_calendars", arguments)

    async def get_calendar_event(**arguments: Any) -> str:
        return await execute("get_event", arguments)

    async def get_today_schedule(**arguments: Any) -> str:
        if captured_request_time is not None:
            arguments["request_time"] = captured_request_time
        return await execute("get_today_schedule", arguments)

    async def get_calendar_schedule(**arguments: Any) -> str:
        return await execute(
            "get_schedule",
            {
                "start_date": arguments.pop("schedule_date"),
                **arguments,
            },
        )

    async def check_calendar_availability(**arguments: Any) -> str:
        return await execute("query_freebusy", arguments)

    tools = []
    if include_event_tools:
        tools.extend([
            StructuredTool.from_function(
                coroutine=list_calendars,
                name="list_calendars",
                description=(
                    "List calendars accessible to the selected connected "
                    "Google account, including their names, IDs, primary "
                    "status, access roles, and timezones."
                ),
                args_schema=ListCalendarsInput,
            ),
            StructuredTool.from_function(
                coroutine=list_calendar_events,
                name="list_calendar_events",
                description=(
                    "List upcoming Google Calendar events, optionally "
                    "filtered by date/time range, text, or attendee. "
                    "Supports pagination."
                ),
                args_schema=ListCalendarEventsInput,
            ),
            StructuredTool.from_function(
                coroutine=get_calendar_event,
                name="get_calendar_event",
                description=(
                    "Retrieve details for one Google Calendar event by ID."
                ),
                args_schema=GetCalendarEventInput,
            ),
            StructuredTool.from_function(
                coroutine=get_today_schedule,
                name="get_today_schedule",
                description=(
                    "Retrieve the selected user's Google Calendar schedule "
                    "for today in an explicit IANA timezone."
                ),
                args_schema=GetTodayScheduleInput,
            ),
            StructuredTool.from_function(
                coroutine=get_calendar_schedule,
                name="get_calendar_schedule",
                description=(
                    "Retrieve a daily, Monday-based weekly, or calendar "
                    "monthly Google Calendar schedule, including existing "
                    "conflict information."
                ),
                args_schema=GetCalendarScheduleInput,
            ),
        ])
    if include_freebusy_tool:
        tools.append(StructuredTool.from_function(
            coroutine=check_calendar_availability,
            name="check_calendar_availability",
            description=(
                "Check free/busy availability for accessible calendars in "
                "an explicit timezone and aware datetime range."
            ),
            args_schema=CheckCalendarAvailabilityInput,
        ))
    return tools
