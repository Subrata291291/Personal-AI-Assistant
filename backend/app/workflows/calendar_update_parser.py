import re
import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.request_metrics import request_id
from app.workflows.calendar_create_draft import extract_explicit_calendar_date

logger = logging.getLogger(__name__)

_TIME = re.compile(
    r"(?<![\w:])(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?"
    r"\s*(?P<meridiem>a\.?m\.?|p\.?m\.?)?(?![\w:])",
    re.IGNORECASE,
)
_SAFE_TIME_ZONE_LOG_VALUE = re.compile(
    r"[A-Za-z0-9_+.-]+(?:/[A-Za-z0-9_+.-]+)+"
)
_TIME_ZONE = re.compile(
    r"\b(?:[A-Za-z0-9_+.-]+/)+[A-Za-z0-9_+.-]+\b"
)
_TITLE_PREFIX = re.compile(
    r"\btitle\s+starts?\s+with\s+['\"“](?P<title>[^'\"”\r\n]+)['\"”]",
    re.IGNORECASE,
)
_TITLE_CHANGE = re.compile(
    r"\b(?:change|set|rename|update)\s+(?:only\s+)?"
    r"(?:(?:the|its|my)\s+)?"
    r"(?:(?:existing\s+)?(?:calendar\s+)?"
    r"(?:meeting|event|appointment)\s+)?"
    r"(?:event\s+)?(?:title|name)\s+(?:to|as)\s*"
    r"(?:['\"“](?P<quoted>[^'\"”\r\n]+)['\"”]|(?P<plain>[^.!?\r\n]+))",
    re.IGNORECASE,
)
_TITLE_ONLY_CHANGE = re.compile(
    r"\b(?:change|set|rename|update|edit|modify)\s+only\s+"
    r"(?:(?:the|its)\s+)?(?:event\s+)?(?:title|name)\b",
    re.IGNORECASE,
)
_FIELD_CHANGE = re.compile(
    r"\b(?:change|set|update|edit|modify)\s+(?:the\s+)?"
    r"(?P<field>location|description)\s+(?:to|as)\s*"
    r"(?:['\"“](?P<quoted>[^'\"”\r\n]+)['\"”]|"
    r"(?P<plain>[^.!?\r\n]+))",
    re.IGNORECASE,
)
_CLOCK_TEXT = (
    r"\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)?"
)
_NATURAL_ORIGINAL_TIME_RANGE = re.compile(
    rf"\b(?:time\s+is|at)\s+(?P<start>{_CLOCK_TEXT})\s*"
    rf"(?:to|[-–—])\s*"
    rf"(?P<end>{_CLOCK_TEXT})",
    re.IGNORECASE,
)
_START_DATE_TIME_RANGE = re.compile(
    rf"\bstart\s+date\s*:\s*[^\r\n]*?\bat\s+"
    rf"(?P<start>{_CLOCK_TEXT})\s*,?\s*end\s+"
    rf"(?P<end>{_CLOCK_TEXT})",
    re.IGNORECASE,
)
_UPDATE_LABELS = (
    "Original start time",
    "Original end time",
    "Proposed start time",
    "Proposed end time",
    "Start time",
    "End time",
    "Event title",
    "Event date",
    "Proposed date",
    "New date",
    "Start date",
    "Original time",
    "Current time",
    "Time",
    "New time",
    "Proposed time",
    "Current start time",
    "Current end time",
    "Timezone",
    "Time zone",
    "Recurrence",
    "Calendar ID",
    "Calendar Id",
    "Event",
    "Title",
    "Date",
    "Calendar",
)
_PRESERVE_TITLE = re.compile(
    r"\b(?:keep|preserve|leave)\b.{0,40}\b(?:the\s+)?same\b"
    r".{0,80}\btitle\b|"
    r"\bdo\s+not\s+change\s+(?:the\s+)?title\b",
    re.IGNORECASE,
)


def _label_value(text: str, *labels: str) -> str | None:
    next_label = "|".join(re.escape(label) for label in _UPDATE_LABELS)
    for label in labels:
        match = re.search(
            rf"(?<!\w){re.escape(label)}\s*:\s*([^\r\n]*?)"
            rf"(?=\r?\n|\s+(?:{next_label})\s*:|\s*$)",
            text,
            re.IGNORECASE,
        )
        if match:
            value = match.group(1).strip().strip(' \t\"\'“”')
            return value.rstrip(".,;") or None
    return None


def _clean_title(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip().strip(' \t\"\'“”')
    value = re.sub(r"[.?!,;]+$", "", value).strip()
    return value or None


def _extract_event_query(text: str) -> str | None:
    title_prefix = _TITLE_PREFIX.search(text)
    if title_prefix:
        return _clean_title(title_prefix.group("title"))

    labeled = _label_value(text, "Event", "Event title", "Title")
    if labeled:
        return _clean_title(labeled)

    quoted = re.search(
        r"\b(?:event|meeting|appointment)\s+"
        r"(?:(?:(?:recurring|calendar)\s+)?series\s+)?"
        r"(?:named|called|titled)\s+[\"“]([^\"”\r\n]+)[\"”]",
        text,
        re.IGNORECASE,
    )
    if quoted:
        return _clean_title(quoted.group(1))

    unquoted = re.search(
        r"\b(?:event|meeting|appointment)\s+"
        r"(?:(?:(?:recurring|calendar)\s+)?series\s+)?"
        r"(?:named|called|titled)\s+(.+?)"
        r"(?=\s+(?:on|from|at|with)\b|\s+to\s+(?=\d)|"
        r"\s+and\s+(?:change|move|reschedule|update|edit|modify)\b|"
        r"[.?!\r\n]|$)",
        text,
        re.IGNORECASE,
    )
    if unquoted:
        return _clean_title(unquoted.group(1))

    recurring_series = re.search(
        r"\brecurring\s+(?P<title>.+?)\s+series\b",
        text,
        re.IGNORECASE,
    )
    if recurring_series:
        return _clean_title(recurring_series.group("title"))

    generic_meeting = re.search(
        r"\b(?:my|the)\s+(?:calendar\s+)?"
        r"(?P<kind>meeting|appointment)\b"
        r"(?:\s+(?:with|about|for)\s+(?P<person>.+?)"
        r"(?=\s+(?:to|on|at|from)\b|[.?!\r\n]|$))?",
        text,
        re.IGNORECASE,
    )
    if generic_meeting:
        person = generic_meeting.group("person")
        return _clean_title(person) if person else generic_meeting.group("kind")

    direct_title = re.search(
        r"\b(?:delete|remove|cancel)\s+(?:my|the)\s+"
        r"(?P<title>[^\r\n]+?)[ \t]*$",
        text,
        re.IGNORECASE | re.MULTILINE,
    )
    if direct_title:
        title = re.sub(
            r"\s+from\s+(?:my\s+)?(?:primary\s+)?"
            r"(?:google\s+)?calendar\b.*$",
            "",
            direct_title.group("title"),
            flags=re.IGNORECASE,
        )
        return _clean_title(title)
    return None


def _extract_title_change(text: str) -> str | None:
    match = _TITLE_CHANGE.search(text)
    if not match:
        return None
    return _clean_title(match.group("quoted") or match.group("plain"))


def _extract_field_changes(text: str) -> dict[str, str]:
    changes: dict[str, str] = {}
    for match in _FIELD_CHANGE.finditer(text):
        raw_value = match.group("quoted") or match.group("plain")
        if match.group("plain"):
            raw_value = re.split(
                r"\s+(?:and\s+)?(?:preserve|keep|leave|prepare|wait\s+for)\b",
                raw_value,
                maxsplit=1,
                flags=re.IGNORECASE,
            )[0]
        value = _clean_title(raw_value)
        if value is not None:
            changes[match.group("field").casefold()] = value
    return changes


def _parse_clock_values(text: str) -> list[time]:
    values: list[time] = []
    for match in _TIME.finditer(text):
        hour = int(match.group("hour"))
        minute = int(match.group("minute") or 0)
        meridiem = (match.group("meridiem") or "").replace(".", "").casefold()
        if match.group("minute") is None and not meridiem:
            continue
        if minute > 59:
            continue
        if meridiem:
            if not 1 <= hour <= 12:
                continue
            hour = hour % 12 + (12 if meridiem == "pm" else 0)
        elif hour > 23:
            continue
        values.append(time(hour, minute))
    return values


def _labeled_time_pair(
    text: str,
    labels: tuple[str, ...],
) -> tuple[time | None, time | None, str | None]:
    value = _label_value(text, *labels)
    if value is None:
        return None, None, None
    clocks = _parse_clock_values(value)
    if len(clocks) != 2:
        return None, None, f"{labels[0].casefold()} must contain two times"
    return clocks[0], clocks[1], None


def _labeled_single_time(
    text: str,
    labels: tuple[str, ...],
) -> tuple[time | None, str | None]:
    value = _label_value(text, *labels)
    if value is None:
        return None, None
    clocks = _parse_clock_values(value)
    if len(clocks) != 1:
        return None, f"{labels[0].casefold()} must contain one time"
    return clocks[0], None


def _aware_local_datetime(
    event_date: date,
    clock: time,
    zone: ZoneInfo,
) -> datetime:
    naive = datetime.combine(event_date, clock)
    first = naive.replace(tzinfo=zone, fold=0)
    second = naive.replace(tzinfo=zone, fold=1)
    if first.utcoffset() != second.utcoffset():
        raise ValueError("The requested local time is ambiguous.")
    if first.astimezone(timezone.utc).astimezone(zone).replace(
        tzinfo=None
    ) != naive:
        raise ValueError("The requested local time does not exist.")
    return first


def _safe_timezone_for_logging(value: object) -> str:
    if (
        isinstance(value, str)
        and len(value) <= 100
        and _SAFE_TIME_ZONE_LOG_VALUE.fullmatch(value)
    ):
        return value
    return "<missing-or-invalid>"


def _safe_diagnostic_token(value: object) -> str:
    if isinstance(value, str) and re.fullmatch(
        r"[A-Za-z0-9_.-]{1,64}",
        value,
    ):
        return value
    return "<invalid>"


def log_calendar_update_diagnostic(
    *,
    boundary: str,
    message_length: int,
    timezone_present: bool,
    module_path: str,
    extracted_timezone: object = None,
    planner_timezone: object = None,
    merged_timezone: object = None,
    validation_result: str = "-",
    date_bounds_timezone: object = None,
    planner_intent: str = "-",
    planner_tool: str = "-",
    planner_arguments_null: bool | None = None,
    selected_route: str = "-",
) -> None:
    """Emit bounded diagnostics without logging user-provided message text."""
    logger.warning(
        "CALENDAR_UPDATE_DIAG_V2 | request_id=%s | boundary=%s "
        "| message_length=%d | timezone_present=%s "
        "| extracted_timezone=%s | planner_timezone=%s "
        "| merged_timezone=%s | validation_result=%s "
        "| date_bounds_timezone=%s | planner_intent=%s "
        "| planner_tool=%s | planner_arguments_null=%s "
        "| selected_route=%s | module_path=%s",
        request_id(),
        _safe_diagnostic_token(boundary),
        message_length,
        timezone_present,
        _safe_timezone_for_logging(extracted_timezone),
        _safe_timezone_for_logging(planner_timezone),
        _safe_timezone_for_logging(merged_timezone),
        _safe_diagnostic_token(validation_result),
        _safe_timezone_for_logging(date_bounds_timezone),
        _safe_diagnostic_token(planner_intent),
        _safe_diagnostic_token(planner_tool),
        planner_arguments_null,
        _safe_diagnostic_token(selected_route),
        module_path,
    )


def _parse_calendar_request_details(
    text: str,
    planner_time_zone: object | None = None,
    reference_time: datetime | None = None,
) -> dict[str, Any]:
    """Extract shared event identity and timing details."""
    event_query = _extract_event_query(text)
    date_text = _label_value(text, "Date", "Event date", "Start date")
    event_date = extract_explicit_calendar_date(
        date_text if date_text is not None else text
    )
    time_zone = _label_value(text, "Timezone", "Time zone")
    if time_zone is None:
        zone_match = _TIME_ZONE.search(text)
        time_zone = zone_match.group(0) if zone_match else None
    zone: ZoneInfo | None = None
    errors: list[str] = []
    if time_zone is None and planner_time_zone is not None:
        if isinstance(planner_time_zone, str):
            time_zone = planner_time_zone
        else:
            errors.append("The timezone is invalid.")
    if date_text is not None and event_date is None:
        errors.append("The event date is invalid or unsupported.")
    if time_zone is not None:
        try:
            zone = ZoneInfo(time_zone)
            time_zone = zone.key
        except (OSError, ValueError, ZoneInfoNotFoundError):
            errors.append("The timezone is invalid.")
            zone = None

    original_start, original_end, original_error = _labeled_time_pair(
        text,
        ("Original time", "Current time", "Time"),
    )
    if original_error:
        errors.append(original_error)
    if original_start is None and original_end is None:
        time_range = _NATURAL_ORIGINAL_TIME_RANGE.search(text)
        if time_range:
            clocks = _parse_clock_values(
                f"{time_range.group('start')} {time_range.group('end')}"
            )
            if len(clocks) == 2:
                original_start, original_end = clocks
        else:
            time_range = _START_DATE_TIME_RANGE.search(text)
            if time_range:
                clocks = _parse_clock_values(
                    f"{time_range.group('start')} {time_range.group('end')}"
                )
                if len(clocks) == 2:
                    original_start, original_end = clocks
    if original_start is None:
        original_start, original_error = _labeled_single_time(
            text,
            ("Original start time", "Current start time", "Start time"),
        )
        if original_error:
            errors.append(original_error)
    if original_end is None:
        original_end, original_error = _labeled_single_time(
            text,
            ("Original end time", "Current end time", "End time"),
        )
        if original_error:
            errors.append(original_error)

    proposed_start, proposed_end, proposed_error = _labeled_time_pair(
        text,
        ("New time", "Proposed time"),
    )
    if proposed_error:
        errors.append(proposed_error)
    if proposed_start is None:
        proposed_start, proposed_error = _labeled_single_time(
            text,
            ("New start time", "Proposed start time"),
        )
        if proposed_error:
            errors.append(proposed_error)
    if proposed_end is None:
        proposed_end, proposed_error = _labeled_single_time(
            text,
            ("New end time", "Proposed end time"),
        )
        if proposed_error:
            errors.append(proposed_error)
    if (
        proposed_start is None
        and proposed_end is None
        and len(_parse_clock_values(text)) < 4
    ):
        moved_time = re.search(
            rf"\b(?:move|reschedule|shift)\b.{{0,100}}?"
            rf"\bto\s+(?P<time>{_CLOCK_TEXT})",
            text,
            re.IGNORECASE,
        )
        if moved_time:
            clocks = _parse_clock_values(moved_time.group("time"))
            if len(clocks) == 1:
                proposed_start = clocks[0]

    if (
        original_start is None
        and original_end is None
        and proposed_start is None
        and proposed_end is None
        and re.search(r"\bfrom\b.+\bto\b", text, re.IGNORECASE | re.DOTALL)
    ):
        clocks = _parse_clock_values(text)
        if len(clocks) == 4:
            original_start, original_end, proposed_start, proposed_end = clocks

    proposed_date = None
    proposed_date_text = _label_value(text, "New date", "Proposed date")
    if proposed_date_text is not None:
        proposed_date = extract_explicit_calendar_date(proposed_date_text)
        if proposed_date is None:
            errors.append("The proposed date is invalid or unsupported.")
    elif proposed_start is not None and reference_time is not None and zone:
        relative_target = re.search(
            rf"\bto\s+{_CLOCK_TEXT}\s+(?P<date>today|tomorrow)\b",
            text,
            re.IGNORECASE,
        )
        if relative_target:
            local_reference = reference_time.astimezone(zone)
            proposed_date = local_reference.date() + timedelta(
                days=1 if relative_target.group("date").casefold() == "tomorrow" else 0
            )

    overnight = bool(re.search(
        r"\b(?:overnight|over\s+night|next\s+day|following\s+day)\b",
        text,
        re.IGNORECASE,
    ))
    lookup_time_min = None
    lookup_time_max = None
    if event_date is not None and zone is not None:
        try:
            lookup_time_min = _aware_local_datetime(event_date, time.min, zone)
            lookup_time_max = _aware_local_datetime(
                event_date + timedelta(days=1),
                time.min,
                zone,
            )
        except ValueError as exc:
            errors.append(str(exc))
    elif event_date is not None:
        errors.append(
            "An explicit IANA timezone is required to match the event date."
        )

    title_change = _extract_title_change(text)
    changes: dict[str, Any] | None = (
        {"summary": title_change} if title_change else None
    )
    explicit_field_changes = _extract_field_changes(text)
    if explicit_field_changes:
        changes = {**(changes or {}), **explicit_field_changes}
    if proposed_start is not None and proposed_end is not None:
        if event_date is None:
            errors.append("A date is required for the proposed time range.")
        elif zone is None:
            errors.append("An explicit IANA timezone is required for the proposed time range.")
        else:
            try:
                start = _aware_local_datetime(event_date, proposed_start, zone)
                end_date = event_date
                end = _aware_local_datetime(end_date, proposed_end, zone)
                if end.astimezone(timezone.utc) <= start.astimezone(timezone.utc):
                    if not overnight:
                        raise ValueError(
                            "The proposed end must be after the proposed start; "
                            "state explicitly if the event is overnight."
                        )
                    end_date += timedelta(days=1)
                    end = _aware_local_datetime(end_date, proposed_end, zone)
                if changes is None:
                    changes = {}
                changes.update({
                    "start": {
                        "dateTime": start.isoformat(timespec="seconds"),
                        "timeZone": zone.key,
                    },
                    "end": {
                        "dateTime": end.isoformat(timespec="seconds"),
                        "timeZone": zone.key,
                    },
                })
            except ValueError as exc:
                errors.append(str(exc))
    elif proposed_start is not None:
        if changes is None:
            changes = {}
        changes["start_time"] = proposed_start.strftime("%H:%M")
        if proposed_date is not None:
            changes["start_date"] = proposed_date.isoformat()

    calendar_id = _label_value(text, "Calendar ID", "Calendar Id")
    calendar_name = _label_value(text, "Calendar")
    if (
        calendar_id is None
        and calendar_name is None
        and re.search(r"\bprimary\s+(?:google\s+)?calendar\b", text, re.IGNORECASE)
    ):
        calendar_id = "primary"
    if calendar_id is None and calendar_name:
        if calendar_name.casefold() in {"primary", "primary calendar"}:
            calendar_id = "primary"
        else:
            calendar_name = _clean_title(calendar_name)
    if calendar_name and calendar_name.casefold() in {
        "primary",
        "primary calendar",
    }:
        calendar_name = None
        calendar_id = "primary"

    missing_fields: list[str] = []
    if not event_query:
        missing_fields.append("event title")
    if event_date is None:
        missing_fields.append("event date")
    if title_change is None:
        if proposed_start is None:
            missing_fields.append("proposed start time")
        if proposed_end is None and proposed_start is None:
            missing_fields.append("proposed end time")
    if (
        (proposed_start is not None or proposed_end is not None or event_date)
        and zone is None
    ):
        missing_fields.append("timezone")

    log_calendar_update_diagnostic(
        boundary="parser_validation",
        message_length=len(text),
        timezone_present=time_zone is not None,
        extracted_timezone=time_zone,
        validation_result="failed" if errors else "passed",
        date_bounds_timezone=(
            zone.key if zone is not None and lookup_time_min is not None else None
        ),
        module_path=__file__,
    )

    return {
        "event_query": event_query,
        "event_date": event_date,
        "original_start_time": original_start,
        "original_end_time": original_end,
        "proposed_start_time": proposed_start,
        "proposed_end_time": proposed_end,
        "time_zone": time_zone,
        "calendar_name": calendar_name,
        "calendar_id": calendar_id,
        "lookup_time_min": lookup_time_min,
        "lookup_time_max": lookup_time_max,
        "changes": changes,
        "overnight": overnight,
        "preserve_summary": bool(_PRESERVE_TITLE.search(text)),
        "event_title_is_explicit": bool(
            _label_value(text, "Event", "Event title", "Title")
            or re.search(
                r"\b(?:delete|remove|cancel)\s+(?:my|the)\s+"
                r"(?!event\b|meeting\b|appointment\b|series\b)",
                text,
                re.IGNORECASE,
            )
            or re.search(
                r"\b(?:event|meeting|appointment)\s+"
                r"(?:(?:(?:recurring|calendar)\s+)?series\s+)?"
                r"(?:named|called|titled)\s+",
                text,
                re.IGNORECASE,
            )
            or re.search(
                r"\brecurring\s+.+?\s+series\b",
                text,
                re.IGNORECASE,
            )
            or _TITLE_PREFIX.search(text)
        ),
        "event_title_is_prefix": bool(_TITLE_PREFIX.search(text)),
        "only_title_change": bool(_TITLE_ONLY_CHANGE.search(text)),
        "missing_fields": missing_fields,
        "errors": errors,
    }


def parse_calendar_delete_request(
    text: str,
    planner_time_zone: object | None = None,
) -> dict[str, Any]:
    """Extract event identity and exact occurrence details for a Delete."""
    details = _parse_calendar_request_details(text, planner_time_zone)
    lowered = text.casefold()
    details["delete_entire_series"] = bool(re.search(
        r"\b(?:entire|whole|complete|full|all)\b.{0,50}"
        r"\b(?:recurring\s+)?series\b|"
        r"\b(?:all\s+future\s+occurrences|entire\s+recurrence)\b",
        lowered,
    ))
    if re.search(
        r"\brrule\s*[:=]\s*(?:freq\s*=\s*)?daily\b|"
        r"\bdaily\b.{0,30}\brecurr(?:ence|ing)\b",
        lowered,
    ):
        details["expected_recurrence"] = ["RRULE:FREQ=DAILY"]
    else:
        details["expected_recurrence"] = None
    details["expected_recurring_unbounded"] = bool(re.search(
        r"\bno\s+end\s+date\b|"
        r"\bwithout\s+(?:an?\s+)?end\s+date\b|"
        r"\bunbounded\b",
        lowered,
    ))

    missing_fields: list[str] = []
    if not details.get("event_query"):
        missing_fields.append("event title")
    if details.get("event_date") is None:
        missing_fields.append("event date")
    if details.get("original_start_time") is None:
        missing_fields.append("start time")
    if details.get("original_end_time") is None:
        missing_fields.append("end time")
    if details.get("time_zone") is None:
        missing_fields.append("timezone")
    if details["expected_recurrence"] is None:
        missing_fields.append("recurrence")
    if not details["expected_recurring_unbounded"]:
        missing_fields.append("no end date")
    details["missing_fields"] = missing_fields
    return details


def parse_calendar_update_message(
    text: str,
    planner_time_zone: object | None = None,
    reference_time: datetime | None = None,
) -> dict[str, Any]:
    """Extract explicit event identity and before/after times for an Update."""
    return _parse_calendar_request_details(
        text,
        planner_time_zone,
        reference_time,
    )


def _valid_query(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value) <= 500
        and not any(ord(character) < 32 for character in value)
    )


def _valid_planner_changes(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict) or not value:
        return None
    allowed = {
        "summary",
        "description",
        "location",
        "start",
        "end",
        "attendees",
        "start_time",
        "duration_delta_minutes",
        "start_date",
    }
    if set(value) - allowed or (("start" in value) != ("end" in value)):
        return None
    if "start_time" in value and (
        not isinstance(value["start_time"], str)
        or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value["start_time"])
    ):
        return None
    if "duration_delta_minutes" in value and (
        isinstance(value["duration_delta_minutes"], bool)
        or not isinstance(value["duration_delta_minutes"], int)
        or not -1440 <= value["duration_delta_minutes"] <= 1440
    ):
        return None
    if "start_date" in value and (
        not isinstance(value["start_date"], str)
        or extract_explicit_calendar_date(value["start_date"]) is None
    ):
        return None
    for key in ("summary", "description", "location"):
        if key in value and value[key] is not None and not isinstance(value[key], str):
            return None
    for key in ("start", "end"):
        if key in value:
            endpoint = value[key]
            if not isinstance(endpoint, dict) or not any(
                isinstance(endpoint.get(field), str)
                for field in ("dateTime", "date")
            ):
                return None
    return dict(value)


def merge_calendar_update_arguments(
    text: str,
    planner_arguments: object,
    *,
    default_time_zone: object | None = None,
    reference_time: datetime | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Merge planner arguments with explicit values recovered from the request."""
    planner = dict(planner_arguments) if isinstance(planner_arguments, dict) else {}
    planner_time_zone = planner.get("time_zone")
    has_explicit_time_zone = bool(
        _label_value(text, "Timezone", "Time zone")
        or _TIME_ZONE.search(text)
    )
    if default_time_zone is not None and not has_explicit_time_zone:
        planner_time_zone = default_time_zone
    elif planner_time_zone is None:
        planner_time_zone = default_time_zone
    details = parse_calendar_update_message(
        text,
        planner_time_zone=planner_time_zone,
        reference_time=reference_time,
    )
    merged = dict(planner)
    if details["time_zone"] is not None:
        merged["time_zone"] = details["time_zone"]

    log_calendar_update_diagnostic(
        boundary="argument_merge",
        message_length=len(text),
        timezone_present=bool(_TIME_ZONE.search(text)),
        extracted_timezone=details["time_zone"],
        planner_timezone=planner.get("time_zone"),
        merged_timezone=merged.get("time_zone"),
        module_path=__file__,
    )

    parsed_query = details["event_query"]
    planner_query = planner.get("event_query")
    if parsed_query and (
        not _valid_query(planner_query)
        or " ".join(planner_query.casefold().split())
        != " ".join(parsed_query.casefold().split())
    ):
        merged["event_query"] = parsed_query
    elif _valid_query(planner_query):
        merged["event_query"] = planner_query.strip()
    else:
        merged.pop("event_query", None)

    if details["calendar_id"]:
        merged["calendar_id"] = details["calendar_id"]
        merged.pop("calendar_name", None)
    elif details["calendar_name"]:
        merged.pop("calendar_id", None)
        merged["calendar_name"] = details["calendar_name"]

    planner_changes = _valid_planner_changes(planner.get("changes")) or {}
    if details["only_title_change"]:
        planner_changes = {}
    parsed_changes = details["changes"] or {}
    planner_changes = {
        key: value
        for key, value in planner_changes.items()
        if key in parsed_changes
    }
    merged_changes = {**planner_changes, **parsed_changes}
    if parsed_changes and ("start" in parsed_changes or "end" in parsed_changes):
        merged_changes.pop("start_time", None)
        merged_changes.pop("duration_delta_minutes", None)
    if details["preserve_summary"]:
        merged_changes.pop("summary", None)
    if merged_changes:
        merged["changes"] = merged_changes
    else:
        merged.pop("changes", None)

    if details["event_date"] is None:
        planner_date = planner.get("event_date") or planner.get("date")
        if isinstance(planner_date, str):
            details["event_date"] = extract_explicit_calendar_date(planner_date)
            if details["event_date"] is not None and details["time_zone"]:
                try:
                    zone = ZoneInfo(details["time_zone"])
                    details["lookup_time_min"] = _aware_local_datetime(
                        details["event_date"],
                        time.min,
                        zone,
                    )
                    details["lookup_time_max"] = _aware_local_datetime(
                        details["event_date"] + timedelta(days=1),
                        time.min,
                        zone,
                    )
                except (ValueError, ZoneInfoNotFoundError) as exc:
                    details["errors"].append(str(exc))
    details["planner_arguments_valid"] = bool(
        _valid_query(planner_query) or planner_changes
    )
    return merged, details