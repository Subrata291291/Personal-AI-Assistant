from datetime import date, datetime, time

from app.workflows import calendar_update_parser
from app.workflows.calendar_update_parser import (
    merge_calendar_update_arguments,
    parse_calendar_delete_request,
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
EXACT_NATURAL_LANGUAGE_REQUEST = (
    "Update the existing Google Calendar event whose title starts with "
    "'A recurring google' and whose time is 10:00 AM–11:00 AM on "
    "November 10, 2026. Change only its title to Gym. Preserve the date, "
    "start time, end time, timezone, calendar, and recurrence. Prepare a "
    "saved Update proposal and wait for explicit approval and separate "
    "Execute."
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


def test_delete_title_without_event_id_is_extracted_exactly():
    details = parse_calendar_delete_request(
        "Delete my Personal AI Assistant - Calendar Test."
    )

    assert details["event_query"] == "Personal AI Assistant - Calendar Test"


def test_delete_parser_does_not_call_update_parser(monkeypatch):
    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("Delete parsing must not invoke Update parsing.")

    monkeypatch.setattr(
        calendar_update_parser,
        "parse_calendar_update_message",
        fail_if_called,
    )

    details = parse_calendar_delete_request(
        "Delete my Personal AI Assistant - Calendar Test."
    )

    assert details["event_query"] == "Personal AI Assistant - Calendar Test"


def test_natural_move_to_single_time_preserves_existing_duration():
    details = parse_calendar_update_message(
        "Move my meeting to 4 PM tomorrow.",
        planner_time_zone="Asia/Kolkata",
    )

    assert details["event_query"] == "meeting"
    assert details["changes"] == {"start_time": "16:00"}
    assert "proposed end time" not in details["missing_fields"]


def test_natural_move_parses_target_day_in_user_timezone():
    details = parse_calendar_update_message(
        "Move my meeting to 4 PM tomorrow.",
        planner_time_zone="Asia/Kolkata",
        reference_time=datetime.fromisoformat("2026-11-08T04:30:00+00:00"),
    )

    assert details["changes"] == {
        "start_time": "16:00",
        "start_date": "2026-11-09",
    }


def test_update_event_identity_extracts_person_without_proposed_time():
    details = parse_calendar_update_message(
        "Move my meeting with Rahul Sharma to 4 PM tomorrow.",
        planner_time_zone="Asia/Kolkata",
    )

    assert details["event_query"] == "Rahul Sharma"
    assert details["changes"] == {"start_time": "16:00"}


def test_title_only_update_extracts_new_title_and_generic_event_identity():
    details = parse_calendar_update_message(
        "Change my meeting title to Project Discussion."
    )

    assert details["event_query"] == "meeting"
    assert details["changes"] == {"summary": "Project Discussion"}


def test_update_extracts_requested_location_and_description_only():
    details = parse_calendar_update_message(
        "Update my meeting named Planning. "
        "Change the location to Conference Room 2 and preserve the time. "
        "Set the description to Quarterly planning review."
    )

    assert details["changes"] == {
        "location": "Conference Room 2",
        "description": "Quarterly planning review",
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


def test_exact_reproduction_extracts_prefix_original_times_and_title_only_change():
    details = parse_calendar_update_message(
        EXACT_NATURAL_LANGUAGE_REQUEST,
        planner_time_zone="Asia/Kolkata",
    )

    assert details["event_query"] == "A recurring google"
    assert details["event_title_is_explicit"] is True
    assert details["event_title_is_prefix"] is True
    assert details["event_date"] == date(2026, 11, 10)
    assert details["original_start_time"] == time(10, 0)
    assert details["original_end_time"] == time(11, 0)
    assert details["time_zone"] == "Asia/Kolkata"
    assert details["changes"] == {"summary": "Gym"}
    assert details["only_title_change"] is True
    assert details["missing_fields"] == []
    assert details["errors"] == []


def test_title_only_update_can_use_the_callers_timezone_for_event_lookup():
    merged, details = merge_calendar_update_arguments(
        EXACT_NATURAL_LANGUAGE_REQUEST,
        {"time_zone": "America/Los_Angeles"},
        default_time_zone="Asia/Kolkata",
    )

    assert merged["time_zone"] == "Asia/Kolkata"
    assert merged["changes"] == {"summary": "Gym"}
    assert details["lookup_time_min"].isoformat() == (
        "2026-11-10T00:00:00+05:30"
    )
    assert details["lookup_time_max"].isoformat() == (
        "2026-11-11T00:00:00+05:30"
    )


def test_title_only_request_discards_unrequested_planner_changes():
    merged, _details = merge_calendar_update_arguments(
        EXACT_NATURAL_LANGUAGE_REQUEST,
        {
            "event_query": "wrong planner target",
            "changes": {
                "summary": "Wrong planner title",
                "location": "Unrequested room",
                "start": {
                    "dateTime": "2026-11-10T12:00:00+05:30",
                    "timeZone": "Asia/Kolkata",
                },
                "end": {
                    "dateTime": "2026-11-10T13:00:00+05:30",
                    "timeZone": "Asia/Kolkata",
                },
            },
        },
        default_time_zone="Asia/Kolkata",
    )

    assert merged["event_query"] == "A recurring google"
    assert merged["changes"] == {"summary": "Gym"}


def test_merge_discards_unrequested_planner_changes():
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
    assert "location" not in merged["changes"]
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


def test_unrequested_planner_changes_are_not_added_to_update_proposal():
    merged, _details = merge_calendar_update_arguments(
        "Move the event from 10:00 AM-10:30 AM to 11:00 AM-11:30 AM "
        "on November 10, 2026. Timezone: Asia/Kolkata.",
        {
            "event_query": "Planning",
            "changes": {"summary": "Planning review", "location": "Room 2"},
        },
    )

    assert merged["event_query"] == "Planning"
    assert merged["changes"] == {
        "start": {
            "dateTime": "2026-11-10T11:00:00+05:30",
            "timeZone": "Asia/Kolkata",
        },
        "end": {
            "dateTime": "2026-11-10T11:30:00+05:30",
            "timeZone": "Asia/Kolkata",
        },
    }
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


def test_recurring_series_delete_request_extracts_exact_lookup_identity():
    details = parse_calendar_delete_request(
        'Delete the entire recurring Google Calendar event series named "Gym". '
        "Calendar: Primary. Start date: November 11, 2026. "
        "Time: 10:00 AM–11:00 AM. Timezone: Asia/Kolkata. "
        "Recurrence: RRULE=DAILY.",
        planner_time_zone="UTC",
    )

    assert details["event_query"] == "Gym"
    assert details["event_title_is_explicit"] is True
    assert details["event_date"] == date(2026, 11, 11)
    assert details["original_start_time"] == time(10, 0)
    assert details["original_end_time"] == time(11, 0)
    assert details["time_zone"] == "Asia/Kolkata"
    assert details["calendar_id"] == "primary"
    assert details["lookup_time_min"].isoformat() == (
        "2026-11-11T00:00:00+05:30"
    )
    assert details["delete_entire_series"] is True
    assert details["expected_recurrence"] == ["RRULE:FREQ=DAILY"]
    assert details["errors"] == []


EXACT_NATURAL_SERIES_DELETE_REQUEST = (
    "Delete the entire recurring Gym series from my primary Google Calendar. "
    "Find the original recurring master event, not an individual occurrence. "
    "Verify that it is the daily recurring series from November 11, 2026, at "
    "10:00 AM to 11:00 AM Asia/Kolkata, with no end date. Show me the event "
    "details and prepare a saved Delete proposal for the entire recurring "
    "series. Wait for my explicit approval and separate Execute action. "
    "Do not delete anything automatically."
)


def test_exact_natural_series_delete_request_extracts_all_safety_details():
    details = parse_calendar_delete_request(
        EXACT_NATURAL_SERIES_DELETE_REQUEST,
        planner_time_zone="UTC",
    )

    assert details["event_query"] == "Gym"
    assert details["event_title_is_explicit"] is True
    assert details["event_date"] == date(2026, 11, 11)
    assert details["original_start_time"] == time(10, 0)
    assert details["original_end_time"] == time(11, 0)
    assert details["time_zone"] == "Asia/Kolkata"
    assert details["calendar_id"] == "primary"
    assert details["delete_entire_series"] is True
    assert details["expected_recurrence"] == ["RRULE:FREQ=DAILY"]
    assert details["expected_recurring_unbounded"] is True
    assert details["missing_fields"] == []
    assert details["errors"] == []