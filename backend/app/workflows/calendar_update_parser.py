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
_UPDATE_LABELS = (
    "Original start time",
    "Original end time",
    "Proposed start time",
    "Proposed end time",
    "Event title",
    "Event date",
    "Original time",
    "Current time",
    "New time",
    "Proposed time",
    "Current start time",
    "Current end time",
    "Timezone",
    "Time zone",
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
    labeled = _label_value(text, "Event", "Event title", "Title")
    if labeled:
        return _clean_title(labeled)

    quoted = re.search(
        r"\b(?:event|meeting|appointment)\s+"
        r"(?:named|called|titled)\s+[\"“]([^\"”\r\n]+)[\"”]",
        text,
        re.IGNORECASE,
    )
    if quoted:
        return _clean_title(quoted.group(1))

    unquoted = re.search(
        r"\b(?:event|meeting|appointment)\s+"
        r"(?:named|called|titled)\s+(.+?)"
        r"(?=\s+(?:on|from|at|with|and\s+"
        r"(?:change|move|reschedule|update|edit|modify))\b|[.?!\r\n]|$)",
        text,
        re.IGNORECASE,
    )
    return _clean_title(unquoted.group(1)) if unquoted else None


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


def parse_calendar_update_message(
    text: str,
    planner_time_zone: object | None = None,
) -> dict[str, Any]:
    """Extract explicit event identity and before/after times for an Update."""
    event_query = _extract_event_query(text)
    date_text = _label_value(text, "Date", "Event date")
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
        ("Original time", "Current time"),
    )
    if original_error:
        errors.append(original_error)
    if original_start is None:
        original_start, original_error = _labeled_single_time(
            text,
            ("Original start time", "Current start time"),
        )
        if original_error:
            errors.append(original_error)
    if original_end is None:
        original_end, original_error = _labeled_single_time(
            text,
            ("Original end time", "Current end time"),
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
        original_start is None
        and original_end is None
        and proposed_start is None
        and proposed_end is None
        and re.search(r"\bfrom\b.+\bto\b", text, re.IGNORECASE | re.DOTALL)
    ):
        clocks = _parse_clock_values(text)
        if len(clocks) == 4:
            original_start, original_end, proposed_start, proposed_end = clocks

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

    changes = None
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
                changes = {
                    "start": {
                        "dateTime": start.isoformat(timespec="seconds"),
                        "timeZone": zone.key,
                    },
                    "end": {
                        "dateTime": end.isoformat(timespec="seconds"),
                        "timeZone": zone.key,
                    },
                }
            except ValueError as exc:
                errors.append(str(exc))

    calendar_id = _label_value(text, "Calendar ID", "Calendar Id")
    calendar_name = _label_value(text, "Calendar")
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
    if proposed_start is None:
        missing_fields.append("proposed start time")
    if proposed_end is None:
        missing_fields.append("proposed end time")
    if (
        (proposed_start is not None or proposed_end is not None or event_date)
        and zone is None
    ):
        missing_fields.append("timezone")

    logger.info(
        "calendar_update_trace | request_id=%s "
        "| boundary=parser_validation | message_length=%d "
        "| timezone_present=%s | extracted_timezone=%s "
        "| validation_result=%s | date_bounds_timezone=%s "
        "| module_path=%s",
        request_id(),
        len(text),
        time_zone is not None,
        _safe_timezone_for_logging(time_zone),
        "failed" if errors else "passed",
        zone.key if zone is not None and lookup_time_min is not None else "-",
        __file__,
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
        "missing_fields": missing_fields,
        "errors": errors,
    }


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
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Merge planner arguments with explicit values recovered from the request."""
    planner = dict(planner_arguments) if isinstance(planner_arguments, dict) else {}
    details = parse_calendar_update_message(
        text,
        planner_time_zone=planner.get("time_zone"),
    )
    merged = dict(planner)
    if details["time_zone"] is not None:
        merged["time_zone"] = details["time_zone"]

    logger.info(
        "calendar_update_trace | request_id=%s "
        "| boundary=argument_merge | message_length=%d "
        "| timezone_present=%s | extracted_timezone=%s "
        "| planner_timezone=%s | merged_timezone=%s | module_path=%s",
        request_id(),
        len(text),
        bool(_TIME_ZONE.search(text)),
        _safe_timezone_for_logging(details["time_zone"]),
        _safe_timezone_for_logging(planner.get("time_zone")),
        _safe_timezone_for_logging(merged.get("time_zone")),
        __file__,
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
    parsed_changes = details["changes"] or {}
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
    details["event_title_is_explicit"] = bool(
        _label_value(text, "Event", "Event title", "Title")
        or re.search(
            r"\b(?:event|meeting|appointment)\s+"
            r"(?:named|called|titled)\s+",
            text,
            re.IGNORECASE,
        )
    )
    details["planner_arguments_valid"] = bool(
        _valid_query(planner_query) or planner_changes
    )
    return merged, details