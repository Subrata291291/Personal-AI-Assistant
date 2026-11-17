import json
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from app.services.calendar_service import CalendarService
from app.workflows.calendar_agent_node import _format_event


def test_normalize_single_event_preserves_fields_and_no_recurrence_inference():
    raw = {
        "id": "single-1",
        "summary": "Daily Standup",  # title must not imply recurrence
        "description": "recurring-ish text should not matter",
        "location": "Room 1",
        "status": "confirmed",
        "etag": '"etag-single"',
        "htmlLink": "https://calendar.google.com/event?eid=single-1",
        "start": {"dateTime": "2026-11-10T10:00:00+05:30", "timeZone": "Asia/Kolkata"},
        "end": {"dateTime": "2026-11-10T10:30:00+05:30", "timeZone": "Asia/Kolkata"},
        "attendees": [{"email": "a@example.com", "displayName": "A"}],
        "updated": "2026-11-01T00:00:00Z",
    }
    ev = CalendarService._normalize_event(raw)
    assert ev["id"] == "single-1"
    assert ev["recurrence"] is None
    assert ev["recurring_event_id"] is None
    assert ev["original_start_time"] is None
    assert ev["start"]["time_zone"] == "Asia/Kolkata"
    assert ev["end"]["time_zone"] == "Asia/Kolkata"
    text = _format_event(ev, "Asia/Kolkata", calendar_id="primary")
    assert "Recurrence: single event" in text
    assert "recurring instance" not in text.lower()


def test_normalize_recurring_master_preserves_recurrence_rule():
    raw = {
        "id": "master-1",
        "summary": "Workout",
        "status": "confirmed",
        "etag": '"etag-master"',
        "start": {"dateTime": "2026-11-10T10:00:00+05:30", "timeZone": "Asia/Kolkata"},
        "end": {"dateTime": "2026-11-10T11:00:00+05:30", "timeZone": "Asia/Kolkata"},
        "recurrence": ["RRULE:FREQ=DAILY;COUNT=5"],
        "attendees": [],
    }
    ev = CalendarService._normalize_event(raw)
    assert ev["id"] == "master-1"
    assert ev["recurrence"] == ["RRULE:FREQ=DAILY;COUNT=5"]
    assert ev["recurring_event_id"] is None
    text = _format_event(ev, "Asia/Kolkata", calendar_id="primary")
    assert "RRULE:FREQ=DAILY;COUNT=5" in text
    assert "single event" not in text.lower()


def test_normalize_recurring_instance_preserves_instance_and_master_refs():
    raw = {
        "id": "master-1_20261110T043000Z",
        "summary": "Workout",
        "status": "confirmed",
        "etag": '"etag-inst"',
        "start": {"dateTime": "2026-11-10T10:00:00+05:30", "timeZone": "Asia/Kolkata"},
        "end": {"dateTime": "2026-11-10T11:00:00+05:30", "timeZone": "Asia/Kolkata"},
        "recurringEventId": "master-1",
        "originalStartTime": {"dateTime": "2026-11-10T10:00:00+05:30", "timeZone": "Asia/Kolkata"},
        "attendees": [],
    }
    ev = CalendarService._normalize_event(raw)
    assert ev["id"] == "master-1_20261110T043000Z"
    assert ev["recurrence"] is None
    assert ev["recurring_event_id"] == "master-1"
    assert ev["original_start_time"] is not None
    assert ev["original_start_time"]["time_zone"] == "Asia/Kolkata"
    text = _format_event(ev, "Asia/Kolkata", calendar_id="primary")
    assert "recurring instance" in text.lower()
    assert "master event id master-1" in text.lower()
    assert "single event" not in text.lower()
