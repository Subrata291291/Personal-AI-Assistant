from __future__ import annotations

from datetime import date as CalendarDate
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class CalendarEventTime(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: CalendarDate | None = None
    date_time: datetime | None = None
    time_zone: str | None = None


class CalendarAttendee(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str | None = None
    display_name: str | None = None
    response_status: str | None = None
    self: bool | None = None
    organizer: bool | None = None


class CalendarEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    summary: str | None = None
    description: str | None = None
    location: str | None = None
    status: str | None = None
    etag: str | None = None
    html_link: str | None = None
    start: CalendarEventTime | None = None
    end: CalendarEventTime | None = None
    attendees: list[CalendarAttendee] = Field(default_factory=list)
    recurrence: list[str] | None = None
    recurring_event_id: str | None = None
    original_start_time: CalendarEventTime | None = None
    updated: str | None = None


class CalendarEventListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    calendar_id: str
    events: list[CalendarEvent]
    next_page_token: str | None = None
    time_zone: str | None = None


class CalendarScheduleResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    calendar_id: str
    date: CalendarDate
    period: str
    time_zone: str
    events: list[CalendarEvent]
    conflicts: list[dict[str, str]]
    conflicts_complete: bool
    next_page_token: str | None = None


class FreeBusyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection_id: UUID
    calendar_ids: list[str] = Field(
        default_factory=lambda: ["primary"],
        min_length=1,
        max_length=50,
    )
    time_min: datetime
    time_max: datetime
    time_zone: str

    @field_validator("time_min", "time_max")
    @classmethod
    def require_aware_datetime(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Datetime values must include a UTC offset.")
        return value

    @model_validator(mode="after")
    def require_ordered_range(self) -> FreeBusyRequest:
        if self.time_max <= self.time_min:
            raise ValueError("time_max must be later than time_min.")
        return self


class FreeBusyInterval(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: datetime
    end: datetime


class FreeBusyError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    domain: str | None = None
    reason: str | None = None


class FreeBusyCalendar(BaseModel):
    model_config = ConfigDict(extra="forbid")

    busy: list[FreeBusyInterval] = Field(default_factory=list)
    errors: list[FreeBusyError] = Field(default_factory=list)


class FreeBusyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    time_min: datetime
    time_max: datetime
    time_zone: str
    calendars: dict[str, FreeBusyCalendar]
