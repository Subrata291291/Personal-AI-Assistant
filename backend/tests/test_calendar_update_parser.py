from datetime import date, time

from app.workflows.calendar_update_parser import (
    merge_calendar_update_arguments,
    parse_calendar_update_message,
)


EXACT_LABELED_REQUEST = (
    "Update my existing Google Calendar event using these details:\n"
    "Event: Personal AI Assistant - Calendar Test\n"
    "Date: November 10, 2026\n"
    "Original time: 10:00 AM-10:30 AM\n"
    "New time: 11:00 AM-11:30 AM\n"
    "Timezone: Asia/Kolkata\n"
    "Calendar: Primary\n"
    "Keep the same date, title, timezone and calendar."
)


def test_labeled_update_request_extracts_event_date_times_zone_and_primary():
    details = parse_calendar_update_message(EXACT_LABELED_REQUEST)

    assert details["event_query"] == "Personal AI Assistant - Calendar Test"
    assert details["event_date"] == date(2026, 11, 10)
    assert details["original_start_time"] == time(10, 0)
    assert details["original_end_time"] == time(10, 30)
    assert details["proposed_start_time"] == time(11, 0)
    assert details["proposed_end_time"] == time(11, 30)
    assert details["time_zone"] == "Asia/Kolkata"
    assert details["calendar_id"] == "primary"
    assert details["calendar_name"] is None
    assert details["preserve_summary"] is True
    assert details["missing_fields"] == []
    assert details["errors"] == []
    assert details["lookup_time_min"].isoformat() == (
        "2026-11-10T00:00:00+05:30"
    )
    assert details["lookup_time_max"].isoformat() == (
        "2026-11-11T00:00:00+05:30"
    )
    assert details["changes"] == {
        "start": {
            "dateTime": "2026-11-10T11:00:00+05:30",
            "timeZone": "Asia/Kolkata",
        },
        "end": {
            "dateTime": "2026-11-10T11:30:00+05:30",
            "timeZone": "Asia/Kolkata",
        },
    }


def test_natural_language_update_parses_24_hour_before_and_after_times():
    details = parse_calendar_update_message(
        'Update my existing event named "Personal AI Assistant - Calendar Test". '
        "Keep the same date, title, timezone and calendar. Change the time "
        "from 10:00–10:30 to 11:00–11:30 on November 10, 2026. "
        "Timezone: Asia/Kolkata."
    )

    assert details["event_query"] == "Personal AI Assistant - Calendar Test"
    assert details["original_start_time"] == time(10, 0)
    assert details["original_end_time"] == time(10, 30)
    assert details["proposed_start_time"] == time(11, 0)
    assert details["proposed_end_time"] == time(11, 30)
    assert details["changes"]["start"]["dateTime"] == (
        "2026-11-10T11:00:00+05:30"
    )
    assert details["preserve_summary"] is True


def test_merge_fills_missing_fields_without_losing_nonconflicting_planner_args():
    merged, details = merge_calendar_update_arguments(
        EXACT_LABELED_REQUEST,
        {
            "event_query": "wrong planner title",
            "calendar_id": "wrong-calendar",
            "time_zone": "America/Los_Angeles",
            "changes": {
                "summary": "Do not apply this title change",
                "location": "Room 3",
            },
        },
    )

    assert merged["event_query"] == "Personal AI Assistant - Calendar Test"
    assert merged["calendar_id"] == "primary"
    assert merged["time_zone"] == "Asia/Kolkata"
    assert merged["changes"]["location"] == "Room 3"
    assert "summary" not in merged["changes"]
    assert merged["changes"]["start"]["dateTime"] == (
        "2026-11-10T11:00:00+05:30"
    )
    assert details["event_date"] == date(2026, 11, 10)


def test_planner_timezone_rebuilds_validation_bounds_and_proposed_changes():
    request_without_timezone = EXACT_LABELED_REQUEST.replace(
        "Timezone: Asia/Kolkata\n",
        "",
    )
    merged, details = merge_calendar_update_arguments(
        request_without_timezone,
        {
            "time_zone": "Asia/Kolkata",
            "changes": {"location": "Room 3"},
        },
    )

    assert merged["time_zone"] == "Asia/Kolkata"
    assert details["time_zone"] == "Asia/Kolkata"
    assert details["errors"] == []
    assert details["missing_fields"] == []
    assert details["lookup_time_min"].isoformat() == (
        "2026-11-10T00:00:00+05:30"
    )
    assert details["lookup_time_max"].isoformat() == (
        "2026-11-11T00:00:00+05:30"
    )
    assert details["changes"] == {
        "start": {
            "dateTime": "2026-11-10T11:00:00+05:30",
            "timeZone": "Asia/Kolkata",
        },
        "end": {
            "dateTime": "2026-11-10T11:30:00+05:30",
            "timeZone": "Asia/Kolkata",
        },
    }


def test_merge_rejects_missing_or_invalid_planner_timezone():
    request_without_timezone = EXACT_LABELED_REQUEST.replace(
        "Timezone: Asia/Kolkata\n",
        "",
    )

    _merged_missing, missing_details = merge_calendar_update_arguments(
        request_without_timezone,
        {},
    )
    _merged_invalid, invalid_details = merge_calendar_update_arguments(
        request_without_timezone,
        {"time_zone": "Not/AZone"},
    )

    assert missing_details["time_zone"] is None
    assert missing_details["lookup_time_min"] is None
    assert missing_details["changes"] is None
    assert "timezone" in missing_details["missing_fields"]
    assert any("explicit IANA timezone is required" in error
               for error in missing_details["errors"])
    assert invalid_details["time_zone"] == "Not/AZone"
    assert invalid_details["lookup_time_min"] is None
    assert invalid_details["changes"] is None
    assert "timezone" in invalid_details["missing_fields"]
    assert "The timezone is invalid." in invalid_details["errors"]


def test_valid_nonconflicting_planner_arguments_are_preserved():
    merged, _details = merge_calendar_update_arguments(
        "Move the event from 10:00 AM-10:30 AM to 11:00 AM-11:30 AM "
        "on November 10, 2026. Timezone: Asia/Kolkata.",
        {
            "event_query": "Planning",
            "changes": {"summary": "Planning review", "location": "Room 2"},
        },
    )

    assert merged["event_query"] == "Planning"
    assert merged["changes"]["summary"] == "Planning review"
    assert merged["changes"]["location"] == "Room 2"
    assert merged["changes"]["start"]["dateTime"] == (
        "2026-11-10T11:00:00+05:30"
    )


def test_missing_timezone_is_not_inferred_and_invalid_time_order_is_rejected():
    missing_zone = parse_calendar_update_message(
        "Update event named Planning from 10:00 AM-10:30 AM to "
        "11:00 AM-11:30 AM on November 10, 2026."
    )
    invalid_order = parse_calendar_update_message(
        "Update event named Planning on November 10, 2026. "
        "Timezone: Asia/Kolkata. New time: 11:00 AM-10:30 AM."
    )

    assert missing_zone["time_zone"] is None
    assert missing_zone["changes"] is None
    assert "timezone" in missing_zone["missing_fields"]
    assert invalid_order["changes"] is None
    assert any("end must be after" in error for error in invalid_order["errors"])


def test_explicit_overnight_update_uses_next_day_end():
    details = parse_calendar_update_message(
        "Update event named Planning on November 10, 2026. "
        "Timezone: Asia/Kolkata. New time: 10:00 PM-1:00 AM overnight."
    )

    assert details["overnight"] is True
    assert details["changes"]["start"]["dateTime"] == (
        "2026-11-10T22:00:00+05:30"
    )
    assert details["changes"]["end"]["dateTime"] == (
        "2026-11-11T01:00:00+05:30"
    )