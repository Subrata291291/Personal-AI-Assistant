import re
from datetime import date, datetime, time as datetime_time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CalendarCreateDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str | None = Field(default=None, max_length=1024)
    event_date: date | None = None
    start_time: str | None = None
    end_time: str | None = None
    time_zone: str | None = Field(default=None, max_length=100)
    calendar_name: str | None = Field(default=None, max_length=1024)
    calendar_id: str | None = Field(default=None, max_length=1024)
    description: str | None = Field(default=None, max_length=8000)
    location: str | None = Field(default=None, max_length=1024)
    attendees: list[str] | None = Field(default=None, max_length=50)
    reminders: list[dict[str, Any]] | None = None
    recurrence: list[str] | None = Field(default=None, max_length=1)
    all_day: bool | None = None
    overnight: bool | None = None
    invalid_fields: list[str] = Field(default_factory=list)

    @field_validator(
        "summary",
        "time_zone",
        "calendar_name",
        "calendar_id",
        "description",
        "location",
    )
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("start_time", "end_time")
    @classmethod
    def validate_clock_value(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = datetime_time.fromisoformat(value)
        return parsed.strftime("%H:%M")


_MONTH_DATE = re.compile(
    r"\b(january|february|march|april|may|june|july|august|"
    r"september|october|november|december)\s+"
    r"(\d{1,2})(?:st|nd|rd|th)?(?:,\s*|\s+)(20\d{2})\b",
    re.IGNORECASE,
)
_ISO_DATE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
_CLOCK = re.compile(
    r"\b(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?\b",
    re.IGNORECASE,
)
_MONTH_NAMES = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)
_FIELD_NAMES = (
    "title", "date", "start time", "end time", "time zone", "timezone",
    "calendar", "description", "location", "attendees", "all day",
    "frequency", "recurrence", "end date", "until", "approval", "execute",
    "execution",
)
_FIELD_LABEL_PATTERN = "|".join(
    re.escape(name) for name in sorted(_FIELD_NAMES, key=len, reverse=True)
)


def _normalize_labeled_text(text: str) -> str:
    text = re.sub(
        rf"\s+[-*•]\s*(?=(?:{_FIELD_LABEL_PATTERN})\s*:)",
        "\n",
        text,
        flags=re.IGNORECASE,
    )
    return re.sub(
        r"\s+(?:[-*•]\s*)?(?=(?:no\s+end\s+date|"
        r"prepare\s+(?:an?\s+)?approval\b|"
        r"wait\s+for\s+(?:explicit\s+)?approval\b|"
        r"approval\s+and\s+separate(?:ly)?\s+execute|"
        r"separate\s+execute)\b)",
        "\n",
        text,
        flags=re.IGNORECASE,
    )


def extract_explicit_calendar_date(text: str) -> date | None:
    iso_match = _ISO_DATE.search(text)
    if iso_match:
        try:
            return date.fromisoformat(iso_match.group(1))
        except ValueError:
            pass

    natural_match = _MONTH_DATE.search(text)
    if not natural_match:
        return None
    try:
        return date(
            int(natural_match.group(3)),
            _MONTH_NAMES.index(natural_match.group(1).casefold()) + 1,
            int(natural_match.group(2)),
        )
    except ValueError:
        return None


def _label_value(text: str, label: str) -> str | None:
    normalized_text = _normalize_labeled_text(text)
    match = re.search(
        rf"^\s*[-*•]?\s*{re.escape(label)}\s*:\s*([^\r\n]*)",
        normalized_text,
        re.IGNORECASE | re.MULTILINE,
    )
    if not match:
        return None
    value = match.group(1).strip()
    return value or None


def _parse_clock_values(text: str) -> list[datetime_time] | None:
    matches = list(_CLOCK.finditer(text))
    if len(matches) > 2:
        return None
    result: list[datetime_time] = []
    for match in matches:
        hour = int(match.group(1))
        minute = int(match.group(2) or 0)
        meridiem = (match.group(3) or "").replace(".", "").casefold()
        if minute > 59:
            return None
        if meridiem:
            if not 1 <= hour <= 12:
                return None
            hour = hour % 12 + (12 if meridiem == "pm" else 0)
        elif hour > 23:
            return None
        result.append(datetime_time(hour, minute))
    return result


def _clock_text_without_dates_and_labels(text: str) -> str:
    cleaned = _ISO_DATE.sub(" ", text)
    cleaned = _MONTH_DATE.sub(" ", cleaned)
    field_pattern = re.compile(
        rf"^\s*[-*•]?\s*(?:{_FIELD_LABEL_PATTERN})"
        r"\s*:\s*[^\r\n]*",
        re.IGNORECASE | re.MULTILINE,
    )
    return field_pattern.sub(" ", _normalize_labeled_text(cleaned))


def extract_calendar_create_draft(
    text: str,
    *,
    default_time_zone: str | None,
    fallback_summary: str | None = None,
    use_default_time_zone: bool = True,
) -> dict:
    values: dict = {}
    invalid_fields: list[str] = []

    title = _label_value(text, "Title")
    if title or fallback_summary:
        summary = title or fallback_summary
        values["summary"] = (
            summary.capitalize()
            if title and summary.islower()
            else summary
        )

    date_text = _label_value(text, "Date")
    explicit_date = extract_explicit_calendar_date(
        date_text if date_text is not None else text
    )
    if explicit_date is not None:
        values["event_date"] = explicit_date
    elif date_text is not None:
        invalid_fields.append("date")

    start_text = _label_value(text, "Start time")
    end_text = _label_value(text, "End time")
    parsed_start = _parse_clock_values(start_text) if start_text else None
    parsed_end = _parse_clock_values(end_text) if end_text else None
    if start_text:
        if parsed_start and len(parsed_start) == 1:
            values["start_time"] = parsed_start[0].strftime("%H:%M")
        else:
            invalid_fields.append("start_time")
    if end_text:
        if parsed_end and len(parsed_end) == 1:
            values["end_time"] = parsed_end[0].strftime("%H:%M")
        else:
            invalid_fields.append("end_time")

    if not start_text and not end_text:
        clocks = _parse_clock_values(_clock_text_without_dates_and_labels(text))
        if clocks is not None:
            if len(clocks) == 2:
                values["start_time"] = clocks[0].strftime("%H:%M")
                values["end_time"] = clocks[1].strftime("%H:%M")
            elif len(clocks) == 1:
                if re.search(r"\b(?:end|finish)\b", text, re.IGNORECASE):
                    values["end_time"] = clocks[0].strftime("%H:%M")
                elif re.search(r"\b(?:start|begin)\b", text, re.IGNORECASE):
                    values["start_time"] = clocks[0].strftime("%H:%M")
                else:
                    values["_unassigned_time"] = clocks[0].strftime("%H:%M")
        elif re.search(r"\b(?:\d{1,2}:\d{2}|\d{1,2}\s*(?:a\.?m\.?|p\.?m\.?))\b", text, re.I):
            invalid_fields.extend(("start_time", "end_time"))

    zone_text = _label_value(text, "Timezone") or _label_value(
        text,
        "Time zone",
    )
    if zone_text is None:
        zone_match = re.search(r"\b[A-Za-z_+-]+/[A-Za-z_+-]+\b", text)
        zone_text = zone_match.group(0) if zone_match else None
    if zone_text is not None:
        try:
            ZoneInfo(zone_text)
            values["time_zone"] = zone_text
        except (ValueError, ZoneInfoNotFoundError):
            invalid_fields.append("time_zone")
    elif use_default_time_zone and default_time_zone:
        values["time_zone"] = default_time_zone

    calendar_name = _label_value(text, "Calendar")
    if calendar_name:
        values["calendar_name"] = calendar_name
        if calendar_name.casefold() in {"primary", "primary calendar"}:
            values["calendar_id"] = "primary"

    for field_name in ("description", "location"):
        field_value = _label_value(text, field_name.title())
        if field_value:
            values[field_name] = field_value

    frequency = _label_value(text, "Frequency") or _label_value(
        text,
        "Recurrence",
    )
    if frequency is None and re.search(
        r"\b(?:daily|every\s+day)\b",
        text,
        re.IGNORECASE,
    ):
        frequency = "daily"
    if frequency is not None:
        normalized_frequency = re.sub(
            r"[\s_-]+",
            "",
            frequency.casefold(),
        )
        if normalized_frequency in {"daily", "everyday"}:
            values["recurrence"] = ["RRULE:FREQ=DAILY"]
        else:
            invalid_fields.append("recurrence")

    attendees = _label_value(text, "Attendees")
    if attendees:
        values["attendees"] = [
            item.strip()
            for item in re.split(r"[,;]", attendees)
            if item.strip()
        ]
    if re.search(r"\ball\s+day\b", text, re.IGNORECASE):
        values["all_day"] = True
    if re.search(
        r"\b(?:overnight|next\s+day|following\s+day)\b",
        text,
        re.IGNORECASE,
    ):
        values["overnight"] = True

    if invalid_fields:
        values["invalid_fields"] = sorted(set(invalid_fields))
    return values


def merge_calendar_create_draft(existing: dict | None, updates: dict) -> dict:
    current = CalendarCreateDraft.model_validate(existing or {})
    updates = dict(updates)
    unassigned_time = updates.pop("_unassigned_time", None)
    if unassigned_time:
        if not current.start_time:
            updates["start_time"] = unassigned_time
        elif not current.end_time:
            updates["end_time"] = unassigned_time
    update = CalendarCreateDraft.model_validate({
        key: value
        for key, value in updates.items()
    })
    merged = current.model_dump(mode="python", exclude_none=True)
    if (
        update.calendar_name
        and update.calendar_name.casefold()
        != (current.calendar_name or "").casefold()
        and "calendar_id" not in update.model_fields_set
    ):
        merged.pop("calendar_id", None)
    for key, value in update.model_dump(mode="python", exclude_none=True).items():
        if key == "invalid_fields":
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, list) and not value:
            continue
        merged[key] = value
    invalid = set(current.invalid_fields) | set(update.invalid_fields)
    valid_fields = set(update.model_fields_set) - {"invalid_fields"}
    invalid.difference_update(valid_fields)
    if "event_date" in valid_fields:
        invalid.discard("date")
    if invalid:
        merged["invalid_fields"] = sorted(invalid)
    else:
        merged.pop("invalid_fields", None)
    return CalendarCreateDraft.model_validate(merged).model_dump(
        mode="json",
        exclude_none=True,
        exclude_defaults=True,
    )


def draft_missing_fields(draft: dict) -> list[str]:
    value = CalendarCreateDraft.model_validate(draft)
    missing = []
    if not value.summary:
        missing.append("title")
    if value.event_date is None:
        missing.append("date")
    if value.all_day is not True:
        if not value.start_time:
            missing.append("start_time")
        if not value.end_time:
            missing.append("end_time")
        if not value.time_zone:
            missing.append("time_zone")
    if not value.calendar_id:
        missing.append("calendar")
    return missing


def draft_to_event_arguments(draft: dict) -> dict | None:
    value = CalendarCreateDraft.model_validate(draft)
    if value.invalid_fields or draft_missing_fields(value.model_dump(mode="python")):
        return None
    if value.all_day:
        start = value.event_date
        end = start + timedelta(days=1)
        time_zone = None
    else:
        zone = ZoneInfo(value.time_zone)
        start = _valid_local_datetime(value.event_date, value.start_time, zone)
        start_clock = datetime_time.fromisoformat(value.start_time)
        end_clock = datetime_time.fromisoformat(value.end_time)
        end_date = value.event_date
        if end_clock <= start_clock:
            if not value.overnight:
                return None
            end_date += timedelta(days=1)
        end = _valid_local_datetime(end_date, value.end_time, zone)
        if start is None or end is None or end <= start:
            return None
        start = start.isoformat(timespec="seconds")
        end = end.isoformat(timespec="seconds")
        time_zone = value.time_zone
    arguments = {
        "calendar_id": value.calendar_id,
        "summary": value.summary,
        "description": value.description,
        "start": start.isoformat() if isinstance(start, date) else start,
        "end": end.isoformat() if isinstance(end, date) else end,
        "time_zone": time_zone,
        "location": value.location,
        "attendees": value.attendees or [],
        "all_day": bool(value.all_day),
    }
    if value.reminders is not None:
        arguments["reminders"] = value.reminders
    if value.recurrence is not None:
        arguments["recurrence"] = value.recurrence
    return arguments


def _valid_local_datetime(
    event_date: date,
    clock_value: str,
    zone: ZoneInfo,
) -> datetime | None:
    naive = datetime.combine(event_date, datetime_time.fromisoformat(clock_value))
    first = naive.replace(tzinfo=zone, fold=0)
    second = naive.replace(tzinfo=zone, fold=1)
    if first.utcoffset() != second.utcoffset():
        return None
    if first.astimezone(ZoneInfo("UTC")).astimezone(zone).replace(
        tzinfo=None
    ) != naive:
        return None
    return first