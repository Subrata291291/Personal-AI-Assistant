import re
import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.request_metrics import request_id
from app.workflows.calendar_create_draft import extract_explicit_calendar_date

logger = logging.getLogger(__name__)

# Bounded window used only for title-only deletes that omit a date. Google's
# default ``timeMin=now`` would otherwise hide an event earlier in the local day.
NO_DATE_LOOKBACK_DAYS = 1
NO_DATE_LOOKAHEAD_DAYS = 30

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

_MONTH_NAMES = (
    r"january|february|march|april|may|june|july|august|"
    r"september|october|november|december|"
    r"jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"
)
_DATE_VALUE = (
    r"\d{4}-\d{2}-\d{2}|"
    r"\d{1,2}/\d{1,2}/\d{2,4}|"
    r"\d{1,2}(?:st|nd|rd|th)?\s+(?:" + _MONTH_NAMES + r")\b(?:,?\s+\d{4})?|"
    r"(?:" + _MONTH_NAMES + r")\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?"
)
_RELATIVE_DAY_VALUE = (
    r"today(?:'s|s)?|tomorrow(?:'s|s)?|yesterday(?:'s|s)?|tonight"
)
_DATE_LANGUAGE = re.compile(
    rf"(?<!\w)(?:(?:on|from|for|during|in)\s+)?"
    rf"(?:(?:{_RELATIVE_DAY_VALUE})|(?:{_DATE_VALUE}))(?!\w)",
    re.IGNORECASE,
)
_TIME_RANGE_IN_TITLE = re.compile(
    rf"{_CLOCK_TEXT}\s*(?:to|[-–—])\s*{_CLOCK_TEXT}",
    re.IGNORECASE,
)
_CLOCK_LANGUAGE = re.compile(
    r"(?<![\w:])\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)(?![\w:])|"
    r"(?<![\w:])\d{1,2}:\d{2}(?![\w:])",
    re.IGNORECASE,
)
_RANGE_CLOCK = (
    r"\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)|\d{1,2}:\d{2}"
)
_GENERIC_TIME_RANGE = re.compile(
    rf"(?<![\w:])(?P<start>{_RANGE_CLOCK})\s*"
    rf"(?:\s*,\s*)?(?:to|[-–—])(?:\s*,\s*)?"
    rf"(?P<end>{_RANGE_CLOCK})(?![\w:])",
    re.IGNORECASE,
)
_CONTEXTUAL_EVENT_REFERENCE = re.compile(
    r"\b(?:event|one)\s+(?:i|you)\s+(?:just\s+)?"
    r"(?:showed|shown|displayed)\b",
    re.IGNORECASE,
)
_CALENDAR_SOURCE_SUFFIX = re.compile(
    r"\s+from\s+(?:(?:my|the)\s+)?(?:primary\s+)?"
    r"(?:google\s+)?calend(?:ar|er)\b.*$",
    re.IGNORECASE,
)
_TITLE_TRAILING_FILLER = re.compile(
    r"(?:\s*\b(?:for|on|at|from|during|in|of)\b\s*)+$",
    re.IGNORECASE,
)
_LEADING_GENERIC_DELETE_TITLE = re.compile(
    r"^\s*(?:(?:the|my|a|an)\s+)*(?:calendar\s+)?"
    r"(?:event|meeting|appointment|session)\s+"
    r"(?:(?:on|from|for|at|about|called|named|titled)\s+)?",
    re.IGNORECASE,
)
_GENERIC_TITLE_QUERY_VALUES = {
    "event", "events", "my event", "the event", "calendar event",
    "meeting", "meetings", "my meeting", "the meeting",
    "appointment", "appointments", "my appointment", "the appointment",
    "session", "my session", "the session",
    "class", "my class", "the class",
    "this", "that", "it", "one", "the one",
}
_BARE_DELETE_PREFIX = re.compile(
    r"^\s*(?:please\s+)?(?:delete|remove|cancel)\s+",
    re.IGNORECASE,
)
_NON_CALENDAR_DELETE_DOMAIN = re.compile(
    r"\b(?:email|emails|gmail|inbox|message|messages|thread|threads|draft|drafts|"
    r"contact|contacts|drive|file|files|document|documents|doc|docs|"
    r"spreadsheet|sheet|note|notes|memory|memories|task|tasks|todo|to-do)\b",
    re.IGNORECASE,
)
_OTHER_MUTATION_ACTION = re.compile(
    r"\b(?:send|reply|forward|draft|create|add|book|schedule|update|edit|"
    r"change|move|reschedule|invite|rsvp|upload|download)\b",
    re.IGNORECASE,
)


def _strip_leading_generic_event_title(value: str) -> str:
    """Drop a generic event noun only when a distinctive title remains.

    Phrases such as "delete the event from 15th november gym time" leave
    "event gym time" after date removal. The generic noun must not be stripped
    from a legitimate one-word title such as "Event Horizon".
    """
    match = _LEADING_GENERIC_DELETE_TITLE.match(value)
    if not match:
        return value
    remainder = value[match.end():].strip()
    if len(remainder.split()) < 2:
        return value
    return remainder


def _strip_title_date_time(value: str | None) -> str | None:
    """Remove date/time language that belongs to lookup bounds, not the title."""
    if not isinstance(value, str):
        return None
    text = _CALENDAR_SOURCE_SUFFIX.sub("", value)
    text = _TIME_RANGE_IN_TITLE.sub(" ", text)
    text = _CLOCK_LANGUAGE.sub(" ", text)
    text = _DATE_LANGUAGE.sub(" ", text)
    text = re.sub(r"\s*(?:,|;|:|\.)\s*", " ", text)
    text = re.sub(r"^\s*[-–—]\s*|\s*[-–—]\s*$", " ", text)
    text = _strip_leading_generic_event_title(text)
    text = _TITLE_TRAILING_FILLER.sub("", text)
    text = re.sub(r"\s+", " ", text).strip(' \t"\'`“”')
    return text or None


def extract_calendar_title_phrase(value: str | None) -> str | None:
    """Return the title-like portion of free text without lookup language."""
    return _strip_title_date_time(value)


def build_calendar_search_query(value: str | None) -> str | None:
    """Derive a Google Calendar ``q`` value from a normalized title phrase."""
    cleaned = _strip_title_date_time(value)
    if not cleaned:
        return None
    normalized = " ".join(cleaned.casefold().split())
    if not normalized or normalized in _GENERIC_TITLE_QUERY_VALUES:
        return None
    return cleaned


def is_bare_calendar_delete_title(text: str) -> bool:
    """Recognize ``delete <title>`` phrasing without a calendar/event noun.

    This is intentionally narrow: it only applies to deletion commands with a
    distinctive title-like payload and no other service domain or action verb.
    """
    if not isinstance(text, str) or not _BARE_DELETE_PREFIX.match(text):
        return False
    if _NON_CALENDAR_DELETE_DOMAIN.search(text):
        return False
    if _OTHER_MUTATION_ACTION.search(text):
        return False
    payload = _BARE_DELETE_PREFIX.sub("", text, count=1)
    cleaned = build_calendar_search_query(payload)
    return bool(cleaned)


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


def _parse_duration_minutes(text: str) -> int | None:
    duration_match = re.search(
        r"\bduration\b.*?\bto\s+(?P<expr>.+?)(?:[.?!]|$)",
        text,
        re.IGNORECASE,
    )
    if duration_match is None:
        duration_match = re.search(
            r"\bduration\b\s*(?::|=)?\s*(?P<expr>.+?)(?:[.?!]|$)",
            text,
            re.IGNORECASE,
        )
    if duration_match is None:
        return None
    expr = duration_match.group("expr")
    hours_match = re.search(r"(\d+)\s*(?:hours?|hrs?|h)\b", expr, re.IGNORECASE)
    minutes_match = re.search(
        r"(\d+)\s*(?:minutes?|mins?|m)\b", expr, re.IGNORECASE
    )
    total = 0
    if hours_match:
        total += int(hours_match.group(1)) * 60
    if minutes_match:
        total += int(minutes_match.group(1))
    if total <= 0:
        # Fall back to a bare number followed by a unit anywhere in the text.
        bare = re.search(
            r"\bto\s+(\d+)\s*(minutes?|mins?|hours?|hrs?)\b",
            text,
            re.IGNORECASE,
        )
        if bare:
            value = int(bare.group(1))
            unit = bare.group(2).casefold()
            total = value * 60 if unit.startswith("hour") or unit.startswith("hr") else value
    return total if total > 0 else None


def _clean_title(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip().strip(' \t\"\'“”')
    value = re.sub(r"[.?!,;]+$", "", value).strip()
    return value or None
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

    if _CONTEXTUAL_EVENT_REFERENCE.search(text):
        return None

    contextual_descriptor = re.search(
        r"\b(?:my|the)\s+"
        r"(?P<descriptor>[A-Za-z][\w'-]*"
        r"(?:\s+(?!title\b|to\b|from\b|on\b|at\b|for\b|with\b|about\b|"
        r"named\b|called\b|titled\b)[A-Za-z][\w'-]*){0,3})\s+"
        r"(?:event|meeting|appointment|session|class|discussion)\b",
        text,
        re.IGNORECASE,
    )
    if contextual_descriptor:
        descriptor = _clean_title(contextual_descriptor.group("descriptor"))
        if (
            descriptor
            and descriptor.casefold() not in {"my", "the", "calendar"}
            and not re.search(
                r"\b(?:calendar|google|existing|update|change)\b",
                descriptor,
                re.IGNORECASE,
            )
        ):
            return descriptor

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
        r"\b(?:delete|remove|cancel)\s+(?:(?:my|the)\s+)?"
        r"(?P<title>[^\r\n]+?)[ \t]*$",
        text,
        re.IGNORECASE | re.MULTILINE,
    )
    if direct_title:
        cleaned = _strip_title_date_time(direct_title.group("title"))
        if not cleaned:
            return None
        normalized = " ".join(cleaned.casefold().split())
        # Demonstratives and contextual references cannot identify a stored
        # title on their own; day-scoped generic discovery is handled
        # separately in parse_calendar_delete_request.
        if normalized in {
            "calendar event",
            "calendar meeting",
            "this",
            "that",
            "it",
            "one",
            "the one",
        } or _CONTEXTUAL_EVENT_REFERENCE.search(text):
            return None
        return cleaned
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
    *,
    allow_bare_time_range: bool = True,
) -> dict[str, Any]:
    """Extract shared event identity and timing details."""
    event_query = _extract_event_query(text)
    date_text = _label_value(text, "Date", "Event date", "Start date")
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
    if time_zone is not None:
        try:
            zone = ZoneInfo(time_zone)
            time_zone = zone.key
        except (OSError, ValueError, ZoneInfoNotFoundError):
            errors.append("The timezone is invalid.")
            zone = None

    parsed_reference_time = reference_time
    if isinstance(parsed_reference_time, str):
        try:
            parsed_reference_time = datetime.fromisoformat(
                parsed_reference_time.replace("Z", "+00:00")
            )
        except Exception:
            parsed_reference_time = None
    reference_date = None
    if isinstance(parsed_reference_time, datetime):
        try:
            reference_date = (
                parsed_reference_time.astimezone(zone).date()
                if zone is not None
                else parsed_reference_time.date()
            )
        except Exception:
            reference_date = None
    elif zone is not None:
        try:
            reference_date = datetime.now(zone).date()
        except Exception:
            reference_date = None

    event_date = extract_explicit_calendar_date(
        date_text if date_text is not None else text,
        reference_date=reference_date,
    )
    if date_text is not None and event_date is None:
        errors.append("The event date is invalid or unsupported.")

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
    if allow_bare_time_range and original_start is None and original_end is None:
        # Date-language can repeat between the two clocks in a user-supplied
        # range ("9:00 AM – Nov 15, 2026, 10:00 AM"). Strip it before matching
        # the generic clock pair so the times still disambiguate candidates.
        date_free_text = _DATE_LANGUAGE.sub(" ", text)
        time_range = _GENERIC_TIME_RANGE.search(date_free_text)
        if time_range:
            clocks = _parse_clock_values(
                f"{time_range.group('start')} {time_range.group('end')}"
            )
            if len(clocks) == 2:
                original_start, original_end = clocks
    if original_start is None:
        original_start, original_error = _labeled_single_time(
            text,
            ("Original start time", "Current start time", "Start time", "Start"),
        )
        if original_error:
            errors.append(original_error)
    if original_end is None:
        original_end, original_error = _labeled_single_time(
            text,
            ("Original end time", "Current end time", "End time", "End"),
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
    duration_minutes = _parse_duration_minutes(text)
    if duration_minutes is not None and original_start is None:
        errors.append("The existing event start time is required.")

    if (
        proposed_start is not None
        and proposed_end is None
        and original_start is not None
        and original_end is not None
    ):
        if original_end <= original_start:
            duration_seconds = (
                datetime.combine(date.min, original_end)
                + timedelta(days=1)
                - datetime.combine(date.min, original_start)
            ).total_seconds()
        else:
            duration_seconds = (
                datetime.combine(date.min, original_end)
                - datetime.combine(date.min, original_start)
            ).total_seconds()
        if duration_seconds > 0:
            proposed_end = (
                datetime.combine(date.min, proposed_start)
                + timedelta(seconds=duration_seconds)
            ).time()

    if (
        duration_minutes is not None
        and original_start is not None
        and proposed_start is None
        and proposed_end is None
    ):
        proposed_start = original_start
        proposed_end = (
            datetime.combine(date.min, original_start)
            + timedelta(minutes=duration_minutes)
        ).time()

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
                r"\b(?:event|meeting|appointment|session|class|discussion)\s+"
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
            or (is_bare_calendar_delete_title(text) and bool(event_query))
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
    reference_time: datetime | None = None,
) -> dict[str, Any]:
    """Extract event identity and details for a Calendar delete request."""
    details = _parse_calendar_request_details(
        text,
        planner_time_zone,
        reference_time=reference_time,
    )
    lowered = text.casefold()
    details["delete_entire_series"] = bool(re.search(
        r"\b(?:entire|whole|complete|full|all)\b.{0,50}"
        r"\b(?:recurring\s+)?series\b|"
        r"\bentire\s+recurrence\b",
        lowered,
    ))
    # Future-only recurrence deletion is deliberately separate from series-wide
    # deletion. It is not implemented and must be rejected before search so a
    # simple confirmation can never escalate into a series delete.
    details["delete_future_occurrences_only"] = bool(re.search(
        r"\b(?:all|every|any)\s+future\s+(?:occurrences?|instances?|events?)\b|"
        r"\b(?:future\s+occurrences?|future\s+instances?)\b|"
        r"\b(?:from\s+now\s+on|going\s+forward|from\s+today\s+onward)\b",
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

    # Resolve common relative day references for simple (non-series) deletes using user's timezone.
    if not details.get("delete_entire_series") and details.get("event_date") is None:
        zone = None
        if isinstance(details.get("time_zone"), str):
            try:
                zone = ZoneInfo(details["time_zone"])
            except Exception:
                zone = None
        ref = reference_time
        if isinstance(ref, str):
            try:
                ref = datetime.fromisoformat(ref.replace("Z", "+00:00"))
            except Exception:
                ref = None
        if zone is not None:
            base = ref.astimezone(zone).date() if isinstance(ref, datetime) else datetime.now(zone).date()
            if re.search(r"\btoday(?:'s|s)?\b", lowered):
                details["event_date"] = base
            elif re.search(r"\btomorrow(?:'s|s)?\b", lowered):
                details["event_date"] = base + timedelta(days=1)
            elif re.search(r"\byesterday(?:'s|s)?\b", lowered):
                details["event_date"] = base - timedelta(days=1)
            if details.get("event_date") is not None and details.get("lookup_time_min") is None:
                try:
                    details["lookup_time_min"] = _aware_local_datetime(details["event_date"], time.min, zone)
                    details["lookup_time_max"] = _aware_local_datetime(details["event_date"] + timedelta(days=1), time.min, zone)
                except Exception as exc:
                    details.setdefault("errors", []).append(str(exc))

    # A distinctive title with no date still needs an explicit local-time
    # window. Without it the service falls back to timeMin=now UTC and an
    # earlier same-local-day event can never be retrieved.
    details["no_date_lookup_window"] = False
    if (
        not details.get("delete_entire_series")
        and not details.get("delete_future_occurrences_only")
        and details.get("event_date") is None
        and details.get("lookup_time_min") is None
        and build_calendar_search_query(details.get("event_query"))
    ):
        zone = None
        if isinstance(details.get("time_zone"), str):
            try:
                zone = ZoneInfo(details["time_zone"])
            except Exception:
                zone = None
        ref = reference_time
        if isinstance(ref, str):
            try:
                ref = datetime.fromisoformat(ref.replace("Z", "+00:00"))
            except Exception:
                ref = None
        if zone is not None:
            base = (
                ref.astimezone(zone).date()
                if isinstance(ref, datetime)
                else datetime.now(zone).date()
            )
            try:
                details["lookup_time_min"] = _aware_local_datetime(
                    base - timedelta(days=NO_DATE_LOOKBACK_DAYS), time.min, zone
                )
                details["lookup_time_max"] = _aware_local_datetime(
                    base + timedelta(days=NO_DATE_LOOKAHEAD_DAYS + 1),
                    time.min,
                    zone,
                )
                details["no_date_lookup_window"] = True
            except Exception as exc:
                details.setdefault("errors", []).append(str(exc))

    missing_fields: list[str] = []
    # Generic deletion (e.g. "delete the event from today's calendar") without a specific title/time
    # should discover/list that day's actual events instead of failing on missing title.
    # Do not convert broad undated requests like "Delete my event." into day discovery.
    generic_event_discovery = False
    if not details.get("delete_entire_series"):
        has_specific_time = bool(details.get("original_start_time") or details.get("original_end_time") or details.get("event_title_is_prefix"))
        eq = details.get("event_query")
        nq = " ".join(str(eq).casefold().split()) if isinstance(eq, str) else ""
        generic_title_like = nq in {"event", "meeting", "appointment", "my event", "the event", "my meeting", "the meeting", "my appointment", "the appointment"}
        if not generic_title_like:
            generic_title_like = bool(re.search(
                r"\b(?:delete|remove|cancel)\b.{0,60}\b(?:the|an?)\s*(?:event|meeting|appointment)\b.{0,30}\b(?:from|on|for)\b.{0,30}\b(?:today|todays|tomorrow|yesterday|calendar)\b",
                lowered,
            ))
        bulk_all_scope = bool(re.search(
            r"\b(?:delete|remove|cancel)\b.{0,20}\b(?:all|every)\b.{0,20}\b(?:events?|meetings?|appointments?)\b|"
            r"\b(?:all|every)\b\s+(?:events?|meetings?|appointments?)\b.{0,20}\b(?:delete|remove|cancel)\b",
            lowered,
        ))
        has_day_scope = details.get("event_date") is not None and bool(re.search(
            r"\b(?:today(?:'s|s)?|tomorrow(?:'s|s)?|yesterday(?:'s|s)?|calendar)\b|"
            r"\b(?:on|from|for)\b.{0,40}\b(?:\d{1,2}(?:st|nd|rd|th)?\s+"
            r"(?:january|february|march|april|may|june|july|august|september|"
            r"october|november|december)|(?:january|february|march|april|may|"
            r"june|july|august|september|october|november|december)\s+"
            r"\d{1,2}(?:st|nd|rd|th)?|20\d{2}-\d{2}-\d{2})\b",
            lowered,
        ))
        if not has_specific_time and has_day_scope and (generic_title_like or bulk_all_scope):
            generic_event_discovery = True
            details["event_query"] = None
            # The query is now intentionally absent; a previously detected
            # "explicit title" flag would otherwise filter out every listed
            # event during candidate matching.
            details["event_title_is_explicit"] = False
            details["event_title_is_prefix"] = False

    if not details.get("event_query") and not generic_event_discovery:
        missing_fields.append("event title")
    if details.get("event_date") is None:
        missing_fields.append("event date")
    if details.get("delete_entire_series"):
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
    elif details.get("time_zone") is None and (re.search(r"\btoday(?:'s|s)?\b|\btomorrow(?:'s|s)?\b|\byesterday(?:'s|s)?\b", lowered)):
        missing_fields.append("timezone")
    details["generic_event_discovery"] = generic_event_discovery
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
        allow_bare_time_range=False,
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