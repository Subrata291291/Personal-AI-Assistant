import re
from datetime import date, datetime, timezone
from typing import Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
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
