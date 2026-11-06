import re
from datetime import date, datetime, timezone
from typing import Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from app.schemas.email_draft import normalize_email_address


_RFC3339_OFFSET = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})$"
)
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class CalendarCreateEventParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    calendar_id: str = Field(min_length=1, max_length=1024)
    summary: str = Field(min_length=1, max_length=1024)
    description: str | None = Field(default=None, max_length=8000)
    start: str = Field(min_length=10, max_length=40)
    end: str = Field(min_length=10, max_length=40)
    time_zone: str | None = Field(default=None, max_length=100)
    location: str | None = Field(default=None, max_length=1024)
    attendees: list[str] = Field(default_factory=list, max_length=50)
    reminders: list[dict[str, JsonValue]] | None = Field(
        default=None,
        max_length=5,
    )
    all_day: bool

    @field_validator("calendar_id", "summary", "description", "location")
    @classmethod
    def reject_control_characters(cls, value: str | None) -> str | None:
        if value is not None and (
            any(ord(character) < 32 and character not in "\r\n\t" for character in value)
            or any(ord(character) == 127 for character in value)
        ):
            raise ValueError("Text fields cannot contain control characters.")
        return value

    @field_validator("summary")
    @classmethod
    def require_nonempty_summary(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("summary cannot be empty.")
        return value.strip()

    @field_validator("attendees")
    @classmethod
    def normalize_attendees(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for item in value:
            address = normalize_email_address(item)
            if address is None:
                raise ValueError("attendees contains an invalid email address.")
            key = address.casefold()
            if key not in seen:
                normalized.append(address)
                seen.add(key)
        return normalized

    @field_validator("reminders")
    @classmethod
    def validate_reminders(
        cls,
        value: list[dict[str, JsonValue]] | None,
    ) -> list[dict[str, JsonValue]] | None:
        if value is None:
            return None
        normalized = []
        for reminder in value:
            if set(reminder) != {"method", "minutes"}:
                raise ValueError(
                    "Each reminder requires only method and minutes."
                )
            method = reminder["method"]
            minutes = reminder["minutes"]
            if method not in {"email", "popup"}:
                raise ValueError("Reminder method must be email or popup.")
            if (
                isinstance(minutes, bool)
                or not isinstance(minutes, int)
                or not 0 <= minutes <= 40_320
            ):
                raise ValueError("Reminder minutes must be between 0 and 40320.")
            normalized.append({"method": method, "minutes": minutes})
        return normalized

    @model_validator(mode="after")
    def validate_times(self) -> Self:
        if self.all_day:
            if self.time_zone is not None:
                raise ValueError(
                    "time_zone must be omitted for all-day events."
                )
            start_date = self._parse_date(self.start, "start")
            end_date = self._parse_date(self.end, "end")
            if end_date <= start_date:
                raise ValueError(
                    "All-day event end must be after its exclusive start date."
                )
            return self

        zone_name = self.time_zone
        if not zone_name:
            raise ValueError("An IANA time_zone is required for timed events.")
        try:
            zone = ZoneInfo(zone_name)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("time_zone must be a valid IANA timezone.") from exc

        start = self._parse_timed_datetime(self.start, "start")
        end = self._parse_timed_datetime(self.end, "end")
        self._validate_zone_offset(start, zone, "start")
        self._validate_zone_offset(end, zone, "end")
        if end.astimezone(timezone.utc) <= start.astimezone(timezone.utc):
            raise ValueError("end must be later than start.")
        return self

    @staticmethod
    def _parse_date(value: str, field_name: str) -> date:
        if not _DATE_ONLY.fullmatch(value):
            raise ValueError(
                f"All-day {field_name} must be a date-only YYYY-MM-DD value."
            )
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{field_name} is not a valid date.") from exc

    @staticmethod
    def _parse_timed_datetime(value: str, field_name: str) -> datetime:
        if not _RFC3339_OFFSET.fullmatch(value):
            raise ValueError(
                f"Timed {field_name} must be RFC3339 with an explicit UTC offset."
            )
        try:
            parsed = datetime.fromisoformat(
                value[:-1] + "+00:00" if value.endswith("Z") else value
            )
        except ValueError as exc:
            raise ValueError(f"{field_name} is not a valid RFC3339 timestamp.") from exc
        if parsed.utcoffset() is None:
            raise ValueError(f"{field_name} must include a UTC offset.")
        return parsed

    @staticmethod
    def _validate_zone_offset(
        value: datetime,
        zone: ZoneInfo,
        field_name: str,
    ) -> None:
        local = value.astimezone(zone)
        if (
            local.replace(tzinfo=None) != value.replace(tzinfo=None)
            or local.utcoffset() != value.utcoffset()
        ):
            raise ValueError(
                f"{field_name} offset does not match time_zone or is "
                "nonexistent in that timezone."
            )


class CalendarUpdateEventParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    calendar_id: str = Field(min_length=1, max_length=1024)
    event_id: str = Field(min_length=1, max_length=1024)
    expected_etag: str = Field(min_length=1, max_length=1024)
    current_event: dict[str, JsonValue]
    changes: dict[str, JsonValue] = Field(min_length=1, max_length=8)
    time_zone: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def validate_update(self) -> Self:
        _validate_calendar_identifiers(self.calendar_id, self.event_id)
        if set(self.changes) - {
            "summary",
            "description",
            "location",
            "start",
            "end",
            "attendees",
        }:
            raise ValueError("Unsupported Calendar event change.")
        if ("start" in self.changes) != ("end" in self.changes):
            raise ValueError("Calendar start and end must be updated together.")
        for key in ("summary", "description", "location"):
            if key not in self.changes:
                continue
            value = self.changes[key]
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{key} must be a string or null.")
            if isinstance(value, str) and (
                len(value) > (1024 if key != "description" else 8000)
                or any(
                    (ord(character) < 32 and character not in "\r\n\t")
                    or ord(character) == 127
                    for character in value
                )
            ):
                raise ValueError(f"{key} contains invalid text.")
        summary = self.changes.get("summary")
        if summary is not None and not summary.strip():
            raise ValueError("summary cannot be empty.")
        attendees = self.changes.get("attendees")
        if attendees is not None:
            if (
                not isinstance(attendees, list)
                or len(attendees) > 50
                or any(not isinstance(item, str) for item in attendees)
            ):
                raise ValueError("attendees must be a list of email addresses.")
            normalized = []
            seen = set()
            for attendee in attendees:
                address = normalize_email_address(attendee)
                if address is None:
                    raise ValueError(
                        "attendees contains an invalid email address."
                    )
                if address.casefold() not in seen:
                    normalized.append(address)
                    seen.add(address.casefold())
            self.changes["attendees"] = normalized
        if "start" in self.changes:
            start = self.changes["start"]
            if not isinstance(start, dict):
                raise ValueError("start and end must be Calendar time objects.")
            if "date" in start:
                if self.time_zone is not None:
                    raise ValueError(
                        "time_zone must be omitted for all-day event changes."
                    )
            else:
                if not self.time_zone:
                    raise ValueError(
                        "A user IANA timezone is required for timed changes."
                    )
                try:
                    ZoneInfo(self.time_zone)
                except (ValueError, ZoneInfoNotFoundError) as exc:
                    raise ValueError("Invalid user timezone.") from exc
            _validate_event_time_pair(
                self.changes["start"],
                self.changes["end"],
                expected_time_zone=self.time_zone,
            )
        _validate_snapshot(self.current_event, self.expected_etag)
        return self


class CalendarDeleteEventParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    calendar_id: str = Field(min_length=1, max_length=1024)
    event_id: str = Field(min_length=1, max_length=1024)
    expected_etag: str = Field(min_length=1, max_length=1024)
    current_event: dict[str, JsonValue]

    @model_validator(mode="after")
    def validate_delete(self) -> Self:
        _validate_calendar_identifiers(self.calendar_id, self.event_id)
        _validate_snapshot(self.current_event, self.expected_etag)
        return self


def _validate_calendar_identifiers(calendar_id: str, event_id: str) -> None:
    if calendar_id in {".", ".."} or not re.fullmatch(
        r"[A-Za-z0-9._@+-]{1,1024}", calendar_id
    ):
        raise ValueError("calendar_id is invalid.")
    if event_id in {".", ".."} or not re.fullmatch(
        r"[A-Za-z0-9._:-]{1,1024}", event_id
    ):
        raise ValueError("event_id is invalid.")


def _validate_event_time_pair(
    start: JsonValue,
    end: JsonValue,
    expected_time_zone: str | None = None,
) -> None:
    if not isinstance(start, dict) or not isinstance(end, dict):
        raise ValueError("start and end must be Calendar time objects.")
    if "date" in start or "date" in end:
        if (
            set(start) != {"date"}
            or set(end) != {"date"}
            or not isinstance(start["date"], str)
            or not isinstance(end["date"], str)
        ):
            raise ValueError("All-day start and end must be date-only.")
        try:
            first = date.fromisoformat(start["date"])
            last = date.fromisoformat(end["date"])
        except ValueError as exc:
            raise ValueError("Invalid all-day event date.") from exc
        if last <= first:
            raise ValueError("All-day end date must be after start date.")
        return
    if (
        not isinstance(start.get("dateTime"), str)
        or not isinstance(end.get("dateTime"), str)
    ):
        raise ValueError("Timed start and end require dateTime values.")
    first = _parse_rfc3339_time(start["dateTime"])
    last = _parse_rfc3339_time(end["dateTime"])
    if last.astimezone(timezone.utc) <= first.astimezone(timezone.utc):
        raise ValueError("Calendar event end must be after its start.")
    for event_time, parsed in ((start, first), (end, last)):
        zone_name = event_time.get("timeZone")
        if expected_time_zone is not None and zone_name != expected_time_zone:
            raise ValueError(
                "Calendar event timezone must match the user's timezone."
            )
        if zone_name is not None:
            if not isinstance(zone_name, str):
                raise ValueError("Calendar event timezone must be a string.")
            try:
                zone = ZoneInfo(zone_name)
            except (ValueError, ZoneInfoNotFoundError) as exc:
                raise ValueError("Invalid Calendar event timezone.") from exc
            if parsed.astimezone(zone).utcoffset() != parsed.utcoffset():
                raise ValueError("Calendar event timezone does not match offset.")


def _parse_rfc3339_time(value: str) -> datetime:
    if not _RFC3339_OFFSET.fullmatch(value):
        raise ValueError("Calendar event times require RFC3339 offsets.")
    try:
        parsed = datetime.fromisoformat(
            value[:-1] + "+00:00" if value.endswith("Z") else value
        )
    except ValueError as exc:
        raise ValueError("Invalid Calendar event time.") from exc
    if parsed.utcoffset() is None:
        raise ValueError("Calendar event times require timezone offsets.")
    return parsed


def _validate_snapshot(
    snapshot: dict[str, JsonValue],
    expected_etag: str,
) -> None:
    allowed = {
        "summary",
        "description",
        "location",
        "start",
        "end",
        "attendees",
        "etag",
    }
    if (
        set(snapshot) - allowed
        or not {"start", "end", "etag"}.issubset(snapshot)
        or snapshot.get("etag") != expected_etag
    ):
        raise ValueError("Current event snapshot is invalid.")
    _validate_event_time_pair(snapshot["start"], snapshot["end"])
    for key in ("summary", "description", "location"):
        value = snapshot.get(key)
        if value is not None and not isinstance(value, str):
            raise ValueError("Current event snapshot contains invalid text.")
    attendees = snapshot.get("attendees")
    if (
        not isinstance(attendees, list)
        or len(attendees) > 50
        or any(not isinstance(item, str) for item in attendees)
    ):
        raise ValueError("Current event snapshot has invalid attendees.")
    if len(repr(snapshot).encode("utf-8")) > 16_000:
        raise ValueError("Current event snapshot is too large.")
