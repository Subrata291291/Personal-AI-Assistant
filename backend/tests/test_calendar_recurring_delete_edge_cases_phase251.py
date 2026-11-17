"""
Phase 2.5.1: Recurrence edge cases for Calendar delete targeting/safety
(mocked/in-process only). No real Google/provider mutations.
"""
from __future__ import annotations

from contextlib import nullcontext
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from app.services.calendar_service import CalendarService
from app.workflows import calendar_agent_node as calendar_agent_module
from app.workflows.calendar_update_parser import parse_calendar_delete_request


USER_ID = UUID("c0cd2d85-c90c-4c62-a886-c41bec1c41af")
CONVERSATION_ID = UUID("c7557e52-cd75-4ea6-9130-e927176518a8")
CAL_CONNECTION_ID = UUID("5a3d3426-f1fd-4cc9-ae83-08181848cde7")
LA = ZoneInfo("America/Los_Angeles")
NY = ZoneInfo("America/New_York")


class _SessionCtx:
    def __enter__(self):
        return object()

    def __exit__(self, *a):
        return False


def _runtime(**overrides):
    ctx = {
        "user_id": str(USER_ID),
        "calendar_write_connection_id": str(CAL_CONNECTION_ID),
        "google_connection_available": True,
        "calendar_enabled": True,
        "calendar_default_connection_invalid": False,
        "calendar_write_selection_required": False,
        "calendar_selected_write_scope_missing": False,
    }
    ctx.update(overrides)
    return SimpleNamespace(context=ctx)


def _delete_state(message: str, **overrides):
    state = {
        "user_message": message,
        "gmail_plan": {
            "intent": "google_calendar",
            "calendar_tool": "delete_calendar_event_proposal",
            "calendar_arguments": None,
        },
        "user_timezone": "America/Los_Angeles",
        "request_time": "2026-11-16T12:00:00Z",
        "assistant_response": "",
        "provider": "",
        "model": "",
        "messages": [],
        "assistant_capabilities": {
            "google_calendar": {
                "connected": True,
                "api_available": True,
                "events_read": True,
                "write_permission_granted": True,
                "delete_execution_available": True,
            }
        },
        "conversation_id": str(CONVERSATION_ID),
        "user_id": str(USER_ID),
    }
    state.update(overrides)
    return state


def _timed_event(
    event_id,
    summary,
    start_dt,
    end_dt,
    *,
    etag="etag-1",
    time_zone="America/Los_Angeles",
    status="confirmed",
    recurrence=None,
    recurring_event_id=None,
    original_start_time=None,
):
    ev = {
        "id": event_id,
        "summary": summary,
        "status": status,
        "etag": etag,
        "start": {"date_time": start_dt, "time_zone": time_zone},
        "end": {"date_time": end_dt, "time_zone": time_zone},
        "attendees": [],
        "description": None,
        "location": None,
        "html_link": f"https://calendar.google.com/event?eid={event_id}",
    }
    if recurrence is not None:
        ev["recurrence"] = recurrence
    if recurring_event_id is not None:
        ev["recurring_event_id"] = recurring_event_id
    if original_start_time is not None:
        ev["original_start_time"] = original_start_time
    return ev


def _service_for(events, *, get_map=None):
    get_map = get_map or {}

    async def _get_event(**kwargs):
        eid = kwargs.get("event_id")
        if eid in get_map:
            return get_map[eid]
        for ev in events:
            if ev.get("id") == eid:
                return ev
        raise AssertionError(f"unexpected get_event {eid}")

    async def _validate_event_write_access(**kwargs):
        return await _get_event(**kwargs)

    return SimpleNamespace(
        list_events=AsyncMock(
            return_value={"events": events, "next_page_token": None, "calendar_id": "primary"}
        ),
        get_event=AsyncMock(side_effect=_get_event),
        validate_event_write_access=AsyncMock(side_effect=_validate_event_write_access),
    )


def _assert_instance_proposal(result, *, expected_event_id, expected_master_id, expected_cal="primary"):
    proposal = result.get("calendar_action_proposal")
    assert isinstance(proposal, dict), result.get("assistant_response")
    params = proposal["parameters"]
    assert params["calendar_id"] == expected_cal
    assert params["event_id"] == expected_event_id
    assert params["delete_entire_series"] is False
    snapshot = params.get("current_event") or {}
    assert snapshot.get("recurrence") in (None, [], False)
    rid = snapshot.get("recurring_event_id") or snapshot.get("recurringEventId")
    ost = snapshot.get("original_start_time") or snapshot.get("originalStartTime")
    if expected_master_id is not None:
        assert rid == expected_master_id, params
    if ost is not None:
        return params, snapshot, ost
    return params, snapshot, ost


# --- Modified recurring instance (actual start differs from original occurrence) ---

def test_modified_recurring_instance_targets_exact_instance_preserves_original_and_actual(monkeypatch):
    """Modified instance: actual start/end moved from original occurrence; delete must target instance and preserve both identities."""
    original_start = datetime(2026, 11, 18, 10, 0, tzinfo=LA)
    modified_start = datetime(2026, 11, 18, 11, 0, tzinfo=LA)  # moved later same day
    instance = _timed_event(
        "inst-mod-1",
        "Daily Standup",
        modified_start,
        modified_start + timedelta(minutes=30),
        etag='"etag-mod"',
        recurring_event_id="master-1",
        original_start_time={"date_time": original_start, "time_zone": "America/Los_Angeles"},
    )
    service = _service_for([instance], get_map={instance["id"]: instance})
    monkeypatch.setattr(calendar_agent_module, "CalendarService", lambda _db: service)
    monkeypatch.setattr(calendar_agent_module, "SessionLocal", _SessionCtx)

    # Request identifies the modified (actual) occurrence time on that date
    msg = (
        "delete the event\n"
        "Event: Daily Standup\n"
        "Date: 2026-11-18\n"
        "Original time: 11:00 AM to 11:30 AM\n"
        "Timezone: America/Los_Angeles"
    )
    result = calendar_agent_module.calendar_agent_node(_delete_state(msg), _runtime())
    params, snapshot, ost = _assert_instance_proposal(
        result, expected_event_id="inst-mod-1", expected_master_id="master-1"
    )
    assert "11:00" in str(snapshot["start"]), snapshot  # actual/modified start preserved in snapshot
    assert ost is not None and "10:00" in str(ost), params  # original occurrence preserved


# --- Cancelled instance (provider status) ---

def test_cancelled_recurring_instance_still_targets_exact_instance_no_master_substitution(monkeypatch):
    """Cancelled instance metadata (if returned/selected) must not cause master/series substitution; target remains explicit."""
    start = datetime(2026, 11, 18, 10, 0, tzinfo=LA)
    instance = _timed_event(
        "inst-cancel-1",
        "Daily Standup",
        start,
        start + timedelta(minutes=30),
        etag='"etag-c"',
        status="cancelled",
        recurring_event_id="master-1",
        original_start_time={"date_time": start, "time_zone": "America/Los_Angeles"},
    )
    service = _service_for([instance], get_map={instance["id"]: instance})
    monkeypatch.setattr(calendar_agent_module, "CalendarService", lambda _db: service)
    monkeypatch.setattr(calendar_agent_module, "SessionLocal", _SessionCtx)

    msg = (
        "delete the event\nEvent: Daily Standup\nDate: 2026-11-18\n"
        "Original time: 10:00 AM to 10:30 AM\nTimezone: America/Los_Angeles"
    )
    result = calendar_agent_module.calendar_agent_node(_delete_state(msg), _runtime())
    # Safe behavior: either propose deleting that exact instance (if allowed) or refuse/clarify; never target master/series.
    if result.get("calendar_action_proposal"):
        params = result["calendar_action_proposal"]["parameters"]
        assert params["event_id"] == "inst-cancel-1"
        assert params["delete_entire_series"] is False
    else:
        resp = (result.get("assistant_response") or "").lower()
        assert "nothing was changed" in resp or "couldn" in resp or "clarif" in resp or "specify" in resp


# --- Missing/conflicting recurring-event linkage ---

def test_missing_recurring_event_id_for_ambiguous_recurring_context_does_not_guess_master(monkeypatch):
    """Missing recurringEventId/linkage with ambiguous recurring context must not infer master/series target."""
    start = datetime(2026, 11, 18, 10, 0, tzinfo=LA)
    # looks like an instance candidate but missing linkage metadata
    ambiguous = _timed_event(
        "evt-amb-1",
        "Daily Standup",
        start,
        start + timedelta(minutes=30),
        etag='"etag-a"',
        recurring_event_id=None,
        original_start_time=None,
    )
    master = _timed_event(
        "master-1",
        "Daily Standup",
        datetime(2026, 11, 10, 10, 0, tzinfo=LA),
        datetime(2026, 11, 10, 10, 30, tzinfo=LA),
        etag='"etag-m"',
        recurrence=["RRULE:FREQ=DAILY"],
    )
    service = _service_for([ambiguous, master], get_map={ambiguous["id"]: ambiguous, master["id"]: master})
    monkeypatch.setattr(calendar_agent_module, "CalendarService", lambda _db: service)
    monkeypatch.setattr(calendar_agent_module, "SessionLocal", _SessionCtx)

    # "series?" ambiguity - must not expand to master delete without explicit verified series intent
    msg = (
        "delete Daily Standup on 2026-11-18 10:00 AM America/Los_Angeles series?\n"
        "Event: Daily Standup\nDate: 2026-11-18\nOriginal time: 10:00 AM to 10:30 AM\nTimezone: America/Los_Angeles"
    )
    result = calendar_agent_module.calendar_agent_node(_delete_state(msg), _runtime())
    if result.get("calendar_action_proposal"):
        params = result["calendar_action_proposal"]["parameters"]
        assert params["delete_entire_series"] is False
        assert params["event_id"] != "master-1"
    else:
        assert "calendar_action_proposal" not in result


# --- Multiple matching occurrences ---

def test_multiple_matching_recurring_occurrences_same_day_still_require_disambiguation(monkeypatch):
    s1 = datetime(2026, 11, 18, 9, 0, tzinfo=LA)
    s2 = datetime(2026, 11, 18, 14, 0, tzinfo=LA)
    i1 = _timed_event("inst-a", "Daily Standup", s1, s1 + timedelta(minutes=30), etag='"e1"', recurring_event_id="m1", original_start_time={"date_time": s1, "time_zone": "America/Los_Angeles"})
    i2 = _timed_event("inst-b", "Daily Standup", s2, s2 + timedelta(minutes=30), etag='"e2"', recurring_event_id="m1", original_start_time={"date_time": s2, "time_zone": "America/Los_Angeles"})
    service = _service_for([i1, i2], get_map={i1["id"]: i1, i2["id"]: i2})
    monkeypatch.setattr(calendar_agent_module, "CalendarService", lambda _db: service)
    monkeypatch.setattr(calendar_agent_module, "SessionLocal", _SessionCtx)

    msg = "delete the event\nEvent: Daily Standup\nDate: 2026-11-18\nTimezone: America/Los_Angeles"
    result = calendar_agent_module.calendar_agent_node(_delete_state(msg), _runtime())
    assert "calendar_action_proposal" not in result
    resp = (result.get("assistant_response") or "").lower()
    assert "multiple" in resp or "which" in resp or "clarif" in resp or "nothing was changed" in resp


# --- Different recurrence frequency / conflicting series verification ---

def test_series_delete_rejects_conflicting_recurrence_frequency(monkeypatch):
    """Series deletion must not proceed when provider master recurrence conflicts with explicitly requested series semantics."""
    instance = _timed_event(
        "inst-1", "Team Sync", datetime(2026, 11, 18, 10, 0, tzinfo=LA), datetime(2026, 11, 18, 10, 30, tzinfo=LA),
        recurring_event_id="master-w", original_start_time={"date_time": datetime(2026, 11, 18, 10, 0, tzinfo=LA), "time_zone": "America/Los_Angeles"},
    )
    master = _timed_event(
        "master-w", "Team Sync", datetime(2026, 11, 11, 10, 0, tzinfo=LA), datetime(2026, 11, 11, 10, 30, tzinfo=LA),
        recurrence=["RRULE:FREQ=WEEKLY"], etag='"em"',
    )
    service = _service_for([master, instance], get_map={instance["id"]: instance, master["id"]: master})
    monkeypatch.setattr(calendar_agent_module, "CalendarService", lambda _db: service)
    monkeypatch.setattr(calendar_agent_module, "SessionLocal", _SessionCtx)

    # user explicitly claims daily/no-end-date but master is weekly -> must refuse destructive series action
    msg = (
        "delete the entire recurring series Team Sync\nEvent: Team Sync\nDate: 2026-11-11\n"
        "Original time: 10:00 AM to 10:30 AM\nTimezone: America/Los_Angeles\ndaily recurrence, no end date"
    )
    result = calendar_agent_module.calendar_agent_node(_delete_state(msg), _runtime())
    assert "calendar_action_proposal" not in result
    resp = (result.get("assistant_response") or "").lower()
    assert "couldn" in resp or "nothing was changed" in resp or "verify" in resp or "clarif" in resp or "specify" in resp


# --- Timezone conversion across date boundaries ---

def test_cross_date_boundary_timezone_preserved_for_instance_delete(monkeypatch):
    """Instance near date boundary: exact target/calendar/tz/occurrence identity preserved (no scope drift)."""
    # 2026-11-18 23:30 LA = 2026-11-19 07:30 UTC; keep instance local identity clear
    start = datetime(2026, 11, 18, 23, 30, tzinfo=LA)
    instance = _timed_event(
        "inst-boundary", "Late Sync", start, start + timedelta(minutes=30),
        etag='"eb"', recurring_event_id="master-b",
        original_start_time={"date_time": start, "time_zone": "America/Los_Angeles"},
    )
    service = _service_for([instance], get_map={instance["id"]: instance})
    monkeypatch.setattr(calendar_agent_module, "CalendarService", lambda _db: service)
    monkeypatch.setattr(calendar_agent_module, "SessionLocal", _SessionCtx)

    msg = (
        "delete the event\nEvent: Late Sync\nDate: 2026-11-18\n"
        "Original time: 11:30 PM to 12:00 AM\nTimezone: America/Los_Angeles"
    )
    result = calendar_agent_module.calendar_agent_node(_delete_state(msg), _runtime())
    # Either exact instance proposal with preserved identity or safe refusal if overnight parsing unsupported; never wrong target/scope.
    if result.get("calendar_action_proposal"):
        params = result["calendar_action_proposal"]["parameters"]
        assert params["event_id"] == "inst-boundary"
        assert params["delete_entire_series"] is False
        ost = (params.get("current_event") or {}).get("original_start_time")
        assert ost is not None
        assert "2026-11-18" in str(ost) or "23:30" in str(ost)
    else:
        resp = (result.get("assistant_response") or "").lower()
        assert "nothing was changed" in resp or "couldn" in resp or "clarif" in resp or "specify" in resp


# --- DST / ambiguous local time handling (safety) ---

def test_ambiguous_local_time_does_not_allow_destructive_series_delete(monkeypatch):
    """Ambiguous or nonexistent local times must not be guessed for destructive scope verification."""
    # Use a message that triggers relative-time/DST-sensitive parsing path or explicit ambiguous intent;
    # at minimum, parser/flow must not produce unsafe series deletion when time cannot be resolved safely.
    # We simulate by requesting series delete with a timezone label that is valid but details missing/ambiguous in effect.
    instance = _timed_event(
        "inst-dst", "DST Event", datetime(2026, 11, 1, 1, 30, tzinfo=NY), datetime(2026, 11, 1, 2, 30, tzinfo=NY),
        recurring_event_id="master-dst", original_start_time={"date_time": datetime(2026, 11, 1, 1, 30, tzinfo=NY), "time_zone": "America/New_York"},
    )
    master = _timed_event(
        "master-dst", "DST Event", datetime(2026, 10, 25, 1, 30, tzinfo=NY), datetime(2026, 10, 25, 2, 30, tzinfo=NY),
        recurrence=["RRULE:FREQ=WEEKLY"], etag='"ed"',
    )
    service = _service_for([master, instance], get_map={instance["id"]: instance, master["id"]: master})
    monkeypatch.setattr(calendar_agent_module, "CalendarService", lambda _db: service)
    monkeypatch.setattr(calendar_agent_module, "SessionLocal", _SessionCtx)

    # incomplete/unsafe time spec for series verification (no explicit unambiguous range) -> refuse
    msg = (
        "delete the entire recurring series DST Event\nEvent: DST Event\nDate: 2026-10-25\n"
        "Timezone: America/New_York\ndaily recurrence, no end date"
    )
    result = calendar_agent_module.calendar_agent_node(_delete_state(msg), _runtime())
    assert "calendar_action_proposal" not in result
    resp = (result.get("assistant_response") or "").lower()
    assert "nothing was changed" in resp or "specify" in resp or "couldn" in resp or "clarif" in resp


# --- Optional/missing recurrence fields in provider payload normalization (non-destructive safety) ---

def test_normalize_event_tolerates_missing_optional_recurrence_fields():
    """Provider payloads may omit optional recurrence-related fields; normalization must not invent them."""
    raw = {
        "id": "evt-plain",
        "summary": "Plain",
        "status": "confirmed",
        "start": {"dateTime": "2026-11-10T10:00:00-08:00", "timeZone": "America/Los_Angeles"},
        "end": {"dateTime": "2026-11-10T10:30:00-08:00", "timeZone": "America/Los_Angeles"},
        # no recurrence, no recurringEventId, no originalStartTime
    }
    ev = CalendarService._normalize_event(raw)
    assert ev["id"] == "evt-plain"
    assert ev["recurrence"] is None
    assert ev["recurring_event_id"] is None
    assert ev["original_start_time"] is None


def test_normalize_event_preserves_recurring_instance_linkage_when_present():
    raw = {
        "id": "inst-1",
        "summary": "Standup",
        "status": "confirmed",
        "start": {"dateTime": "2026-11-18T10:00:00-08:00", "timeZone": "America/Los_Angeles"},
        "end": {"dateTime": "2026-11-18T10:30:00-08:00", "timeZone": "America/Los_Angeles"},
        "recurringEventId": "master-1",
        "originalStartTime": {"dateTime": "2026-11-18T10:00:00-08:00", "timeZone": "America/Los_Angeles"},
    }
    ev = CalendarService._normalize_event(raw)
    assert ev["recurring_event_id"] == "master-1"
    assert ev["original_start_time"] is not None
    assert ev["recurrence"] is None
