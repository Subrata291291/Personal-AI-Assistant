import asyncio
from contextlib import nullcontext
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from app.schemas.action_approval import ActionApprovalProposal
from app.schemas.calendar_approval import CalendarDeleteEventParameters
from app.services.calendar_action_executor import CalendarActionExecutor
from app.services.calendar_action_identity import build_calendar_operation_identity
import app.services.calendar_action_executor as cae


def _delete_identity(params):
    return build_calendar_operation_identity(
        proposal_id=PROPOSAL_ID,
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        action_type="calendar.delete_event",
        parameters=cae._calendar_identity_parameters(params),
    )
from app.workflows import calendar_agent_node as calendar_agent_module
from app.workflows import gmail_agent_nodes
from app.workflows.calendar_agent_node import calendar_agent_node as run_node
from app.workflows.calendar_update_parser import parse_calendar_delete_request

USER_ID = UUID("c0cd2d85-c90c-4c62-a886-c41bec1c41af")
CONNECTION_ID = UUID("5a3d3426-f1fd-4cc9-ae83-08181848cde7")
CONVERSATION_ID = UUID("11111111-2222-3333-4444-555555555555")
PROPOSAL_ID = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")


def _runtime(**overrides):
    ctx = {
        "user_id": str(USER_ID),
        "calendar_write_connection_id": str(CONNECTION_ID),
        "google_connection_available": True,
        "calendar_enabled": True,
        "contacts_connection_id": None,
        "drive_connection_id": None,
    }
    ctx.update(overrides)
    return SimpleNamespace(context=ctx)


def _state(
    message: str,
    *,
    timezone_name="America/Los_Angeles",
    request_time="2026-11-16T12:00:00Z",
    plan=None,
    assistant_capabilities=None,
):
    return {
        "user_message": message,
        "gmail_plan": plan
        or {
            "intent": "google_calendar",
            "calendar_tool": "delete_calendar_event_proposal",
            "calendar_arguments": None,
        },
        "user_timezone": timezone_name,
        "request_time": request_time,
        "assistant_response": "",
        "provider": "",
        "model": "",
        "messages": [],
        "assistant_capabilities": assistant_capabilities
        or {
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


def _event(event_id, summary, start_dt, end_dt, *, etag="etag-1", time_zone="America/Los_Angeles"):
    return {
        "id": event_id,
        "summary": summary,
        "status": "confirmed",
        "etag": etag,
        "start": {"date_time": start_dt, "time_zone": time_zone},
        "end": {"date_time": end_dt, "time_zone": time_zone},
        "attendees": [],
        "description": None,
        "location": None,
        "html_link": f"https://calendar.google.com/event?eid={event_id}",
    }


class _SessionCtx:
    def __enter__(self):
        return object()

    def __exit__(self, *a):
        return False


def test_today_delete_parser_uses_reference_time_and_user_timezone():
    details = parse_calendar_delete_request(
        "delete the Meeting time from todays calender",
        planner_time_zone="America/Los_Angeles",
        reference_time="2026-11-16T12:00:00Z",
    )
    assert details["event_date"] == date(2026, 11, 16)
    assert details["time_zone"] == "America/Los_Angeles"
    assert details["lookup_time_min"].tzinfo.key == "America/Los_Angeles"
    assert details["lookup_time_max"].tzinfo.key == "America/Los_Angeles"
    assert details["lookup_time_min"].date() == date(2026, 11, 16)
    assert details["lookup_time_max"].date() == date(2026, 11, 17)
    assert details["missing_fields"] == []


def test_today_delete_parser_timezone_boundary_uses_user_local_day():
    # 2026-11-17T02:30:00Z == 2026-11-16 in America/Los_Angeles (UTC-8 in November)
    details = parse_calendar_delete_request(
        "delete today's meeting",
        planner_time_zone="America/Los_Angeles",
        reference_time="2026-11-17T02:30:00Z",
    )
    assert details["event_date"] == date(2026, 11, 16)


def test_delete_today_meeting_invokes_list_events_with_correct_bounds_and_context(monkeypatch):
    tz = ZoneInfo("America/Los_Angeles")
    meeting = _event(
        "evt-meeting-1",
        "Meeting",
        datetime(2026, 11, 16, 10, 0, tzinfo=tz),
        datetime(2026, 11, 16, 11, 0, tzinfo=tz),
        time_zone="America/Los_Angeles",
    )
    service = SimpleNamespace(
        list_events=AsyncMock(
            return_value={"events": [meeting], "next_page_token": None, "calendar_id": "primary"}
        ),
        get_event=AsyncMock(return_value=meeting),
        validate_event_write_access=AsyncMock(return_value=meeting),
    )
    monkeypatch.setattr(calendar_agent_module, "CalendarService", lambda _db: service)
    monkeypatch.setattr(calendar_agent_module, "SessionLocal", _SessionCtx)

    state = _state("delete the Meeting time from todays calender")
    result = run_node(state, _runtime())

    assert service.list_events.await_count >= 1
    call = service.list_events.await_args_list[0]
    assert call.kwargs["user_id"] == USER_ID
    assert call.kwargs["connection_id"] == CONNECTION_ID
    assert call.kwargs["calendar_id"] == "primary"
    assert call.kwargs["time_min"].date() == date(2026, 11, 16)
    assert call.kwargs["time_max"].date() == date(2026, 11, 17)
    assert str(call.kwargs["time_min"].tzinfo) == "America/Los_Angeles"
    assert str(call.kwargs["time_max"].tzinfo) == "America/Los_Angeles"
    assert call.kwargs["time_zone"] == "America/Los_Angeles"

    proposal = result.get("calendar_action_proposal")
    assert isinstance(proposal, dict)
    assert proposal["action_type"] == "calendar.delete_event"
    params = proposal["parameters"]
    assert params["calendar_id"] == "primary"
    assert params["event_id"] == "evt-meeting-1"
    service.validate_event_write_access.assert_awaited()


def test_delete_matching_accepts_meeting_time_query_against_meeting_summary(monkeypatch):
    tz = ZoneInfo("America/Los_Angeles")
    meeting = _event(
        "evt-meeting-2",
        "Meeting",
        datetime(2026, 11, 16, 14, 0, tzinfo=tz),
        datetime(2026, 11, 16, 15, 0, tzinfo=tz),
        time_zone="America/Los_Angeles",
    )
    service = SimpleNamespace(
        list_events=AsyncMock(
            return_value={"events": [meeting], "next_page_token": None, "calendar_id": "primary"}
        ),
        get_event=AsyncMock(return_value=meeting),
        validate_event_write_access=AsyncMock(return_value=meeting),
    )
    monkeypatch.setattr(calendar_agent_module, "CalendarService", lambda _db: service)
    monkeypatch.setattr(calendar_agent_module, "SessionLocal", _SessionCtx)

    result = run_node(_state("delete the Meeting time from todays calender"), _runtime())
    assert (
        result.get("calendar_action_proposal", {}).get("parameters", {}).get("event_id")
        == "evt-meeting-2"
    )


def test_delete_matching_accepts_meeting_time_summary(monkeypatch):
    tz = ZoneInfo("America/Los_Angeles")
    meeting = _event(
        "evt-meeting-3",
        "Meeting time",
        datetime(2026, 11, 16, 9, 0, tzinfo=tz),
        datetime(2026, 11, 16, 9, 30, tzinfo=tz),
        time_zone="America/Los_Angeles",
    )
    service = SimpleNamespace(
        list_events=AsyncMock(
            return_value={"events": [meeting], "next_page_token": None, "calendar_id": "primary"}
        ),
        get_event=AsyncMock(return_value=meeting),
        validate_event_write_access=AsyncMock(return_value=meeting),
    )
    monkeypatch.setattr(calendar_agent_module, "CalendarService", lambda _db: service)
    monkeypatch.setattr(calendar_agent_module, "SessionLocal", _SessionCtx)

    result = run_node(_state("Delete my meeting today"), _runtime())
    assert (
        result.get("calendar_action_proposal", {}).get("parameters", {}).get("event_id")
        == "evt-meeting-3"
    )


def test_delete_multiple_matches_requests_clarification_without_proposal(monkeypatch):
    tz = ZoneInfo("America/Los_Angeles")
    e1 = _event(
        "evt-a",
        "Meeting",
        datetime(2026, 11, 16, 9, 0, tzinfo=tz),
        datetime(2026, 11, 16, 10, 0, tzinfo=tz),
        time_zone="America/Los_Angeles",
    )
    e2 = _event(
        "evt-b",
        "Meeting with team",
        datetime(2026, 11, 16, 11, 0, tzinfo=tz),
        datetime(2026, 11, 16, 12, 0, tzinfo=tz),
        time_zone="America/Los_Angeles",
    )
    service = SimpleNamespace(
        list_events=AsyncMock(
            return_value={"events": [e1, e2], "next_page_token": None, "calendar_id": "primary"}
        ),
        get_event=AsyncMock(),
        validate_event_write_access=AsyncMock(),
    )
    monkeypatch.setattr(calendar_agent_module, "CalendarService", lambda _db: service)
    monkeypatch.setattr(calendar_agent_module, "SessionLocal", _SessionCtx)

    result = run_node(_state("delete today's meeting"), _runtime())
    assert "calendar_action_proposal" not in result
    resp = (result.get("assistant_response") or "").lower()
    # Must not arbitrarily delete; must ask user to identify/disambiguate (wording may vary)
    assert ("multiple" in resp) or ("which calendar event" in resp) or ("which one" in resp)
    service.validate_event_write_access.assert_not_awaited()
    # No delete execution/proposal prepared
    assert "nothing was changed" in resp or "which" in resp


def test_delete_no_match_reports_accurate_result(monkeypatch):
    service = SimpleNamespace(
        list_events=AsyncMock(return_value={"events": [], "next_page_token": None, "calendar_id": "primary"}),
        get_event=AsyncMock(),
        validate_event_write_access=AsyncMock(),
    )
    monkeypatch.setattr(calendar_agent_module, "CalendarService", lambda _db: service)
    monkeypatch.setattr(calendar_agent_module, "SessionLocal", _SessionCtx)

    result = run_node(_state("delete the Meeting time from todays calender"), _runtime())
    assert "calendar_action_proposal" not in result
    resp = (result.get("assistant_response") or "").lower()
    assert "couldn’t find" in resp or "couldn't find" in resp


def test_delete_event_outside_today_bounds_not_selected(monkeypatch):
    tz = ZoneInfo("America/Los_Angeles")
    tomorrow = _event(
        "evt-tomorrow",
        "Meeting",
        datetime(2026, 11, 17, 10, 0, tzinfo=tz),
        datetime(2026, 11, 17, 11, 0, tzinfo=tz),
        time_zone="America/Los_Angeles",
    )
    service = SimpleNamespace(
        list_events=AsyncMock(
            return_value={"events": [tomorrow], "next_page_token": None, "calendar_id": "primary"}
        ),
        get_event=AsyncMock(),
        validate_event_write_access=AsyncMock(),
    )
    monkeypatch.setattr(calendar_agent_module, "CalendarService", lambda _db: service)
    monkeypatch.setattr(calendar_agent_module, "SessionLocal", _SessionCtx)

    result = run_node(_state("delete today's meeting"), _runtime())
    assert "calendar_action_proposal" not in result


def test_calendar_delete_request_routes_to_calendar_not_gmail():
    state = {
        "user_message": "delete the Meeting time from todays calender",
        "messages": [],
        "user_timezone": "America/Los_Angeles",
        "request_time": "2026-11-16T12:00:00Z",
        "assistant_capabilities": {
            "google_calendar": {
                "api_available": True,
                "events_read": True,
                "connected": True,
                "write_permission_granted": True,
                "delete_execution_available": True,
            },
            "gmail": {"connected": True, "api_available": True},
        },
    }
    out = gmail_agent_nodes.gmail_intent_router_node(state)
    assert out.get("assistant_route") == "google_calendar"
    assert (out.get("gmail_plan") or {}).get("calendar_tool") == "delete_calendar_event_proposal"
    assert (out.get("gmail_plan") or {}).get("intent") == "google_calendar"


def test_generic_delete_without_title_lists_today_events_and_stores_trusted_selection(monkeypatch):
    tz = ZoneInfo("America/Los_Angeles")
    e1 = _event("evt-1", "Team Meeting", datetime(2026, 11, 16, 10, 0, tzinfo=tz), datetime(2026, 11, 16, 11, 0, tzinfo=tz), time_zone="America/Los_Angeles")
    e2 = _event("evt-2", "Client Call", datetime(2026, 11, 16, 15, 0, tzinfo=tz), datetime(2026, 11, 16, 15, 30, tzinfo=tz), time_zone="America/Los_Angeles")
    service = SimpleNamespace(
        list_events=AsyncMock(return_value={"events": [e1, e2], "next_page_token": None, "calendar_id": "primary"}),
        get_event=AsyncMock(),
        validate_event_write_access=AsyncMock(),
    )
    monkeypatch.setattr(calendar_agent_module, "CalendarService", lambda _db: service)
    monkeypatch.setattr(calendar_agent_module, "SessionLocal", _SessionCtx)

    result = run_node(_state("delete the event from todays calender"), _runtime())

    assert service.list_events.await_count >= 1
    call = service.list_events.await_args_list[0]
    assert call.kwargs["query"] is None  # generic discovery without explicit title
    assert call.kwargs["user_id"] == USER_ID
    assert call.kwargs["connection_id"] == CONNECTION_ID
    assert call.kwargs["calendar_id"] == "primary"
    assert call.kwargs["time_min"].date() == date(2026, 11, 16)
    assert call.kwargs["time_max"].date() == date(2026, 11, 17)

    assert "calendar_action_proposal" not in result
    resp = result.get("assistant_response") or ""
    assert "multiple events" in resp.lower() or "which one" in resp.lower()
    assert "Team Meeting" in resp and "Client Call" in resp
    sel = result.get("pending_calendar_delete_selection")
    assert isinstance(sel, dict)
    assert sel.get("operation") == "delete_selection"
    assert sel.get("user_id") == str(USER_ID)
    assert sel.get("conversation_id") == str(CONVERSATION_ID)
    assert sel.get("calendar_id") == "primary"
    ids = [c.get("event_id") for c in (sel.get("candidates") or [])]
    assert ids == ["evt-1", "evt-2"]
    service.validate_event_write_access.assert_not_awaited()


def test_generic_delete_without_title_single_event_creates_delete_proposal_only(monkeypatch):
    tz = ZoneInfo("America/Los_Angeles")
    e1 = _event("evt-solo", "Solo Event", datetime(2026, 11, 16, 10, 0, tzinfo=tz), datetime(2026, 11, 16, 11, 0, tzinfo=tz), time_zone="America/Los_Angeles")
    service = SimpleNamespace(
        list_events=AsyncMock(return_value={"events": [e1], "next_page_token": None, "calendar_id": "primary"}),
        get_event=AsyncMock(return_value=e1),
        validate_event_write_access=AsyncMock(return_value=e1),
    )
    monkeypatch.setattr(calendar_agent_module, "CalendarService", lambda _db: service)
    monkeypatch.setattr(calendar_agent_module, "SessionLocal", _SessionCtx)

    result = run_node(_state("delete the event from todays calender"), _runtime())
    assert service.list_events.await_args_list[0].kwargs["query"] is None
    proposal = result.get("calendar_action_proposal")
    assert isinstance(proposal, dict) and proposal["action_type"] == "calendar.delete_event"
    assert proposal["parameters"]["event_id"] == "evt-solo"
    assert proposal["parameters"]["calendar_id"] == "primary"
    # No execution performed by agent; proposal requires approval flow.
    assert not hasattr(service, "delete_event") or not getattr(service.delete_event, "await_count", 0)


def test_generic_delete_without_title_zero_events_reports_none_found(monkeypatch):
    service = SimpleNamespace(
        list_events=AsyncMock(return_value={"events": [], "next_page_token": None, "calendar_id": "primary"}),
        get_event=AsyncMock(),
        validate_event_write_access=AsyncMock(),
    )
    monkeypatch.setattr(calendar_agent_module, "CalendarService", lambda _db: service)
    monkeypatch.setattr(calendar_agent_module, "SessionLocal", _SessionCtx)

    result = run_node(_state("delete the event from todays calender"), _runtime())
    assert service.list_events.await_args_list[0].kwargs["query"] is None
    assert "calendar_action_proposal" not in result
    resp = (result.get("assistant_response") or "").casefold()
    assert "any events" in resp or "no events" in resp
    assert "today" in resp


def test_ordinal_followup_delete_second_one_resolves_to_trusted_event_id():
    state = {
        "user_message": "delete the second one",
        "messages": [],
        "user_timezone": "America/Los_Angeles",
        "request_time": "2026-11-16T12:00:00Z",
        "user_id": str(USER_ID),
        "conversation_id": str(CONVERSATION_ID),
        "assistant_capabilities": {
            "google_calendar": {
                "api_available": True, "events_read": True, "connected": True,
                "write_permission_granted": True, "delete_execution_available": True,
            },
        },
        "pending_calendar_delete_selection": {
            "version": 1,
            "operation": "delete_selection",
            "user_id": str(USER_ID),
            "conversation_id": str(CONVERSATION_ID),
            "connection_id": str(CONNECTION_ID),
            "calendar_id": "primary",
            "candidates": [
                {"event_id": "evt-1", "calendar_id": "primary", "summary": "Team Meeting"},
                {"event_id": "evt-2", "calendar_id": "primary", "summary": "Client Call"},
            ],
            "created_at": "2026-11-16T12:00:00+00:00",
            "expires_at": "2099-01-01T00:00:00+00:00",
        },
    }
    out = gmail_agent_nodes.gmail_intent_router_node(state)
    assert out.get("assistant_route") == "google_calendar"
    plan = out.get("gmail_plan") or {}
    assert plan.get("calendar_tool") == "delete_calendar_event_proposal"
    args = plan.get("calendar_arguments") or {}
    assert args.get("event_id") == "evt-2"
    assert args.get("calendar_id") == "primary"
    assert out.get("pending_calendar_delete_selection") is None


def test_generic_delete_multiple_events_then_ordinal_creates_proposal_for_correct_event(monkeypatch):
    tz = ZoneInfo("America/Los_Angeles")
    e1 = _event("evt-1", "Team Meeting", datetime(2026, 11, 16, 10, 0, tzinfo=tz), datetime(2026, 11, 16, 11, 0, tzinfo=tz), time_zone="America/Los_Angeles")
    e2 = _event("evt-2", "Client Call", datetime(2026, 11, 16, 15, 0, tzinfo=tz), datetime(2026, 11, 16, 15, 30, tzinfo=tz), time_zone="America/Los_Angeles")
    service = SimpleNamespace(
        list_events=AsyncMock(return_value={"events": [e1, e2], "next_page_token": None, "calendar_id": "primary"}),
        get_event=AsyncMock(return_value=e2),
        validate_event_write_access=AsyncMock(return_value=e2),
    )
    monkeypatch.setattr(calendar_agent_module, "CalendarService", lambda _db: service)
    monkeypatch.setattr(calendar_agent_module, "SessionLocal", _SessionCtx)

    first = run_node(_state("delete the event from todays calender"), _runtime())
    sel = first.get("pending_calendar_delete_selection")
    assert isinstance(sel, dict) and [c["event_id"] for c in sel["candidates"]] == ["evt-1", "evt-2"]

    follow_state = _state(
        "Delete the selected calendar event (event_id=evt-2, calendar_id=primary).",
        plan={"intent": "google_calendar", "calendar_tool": "delete_calendar_event_proposal", "calendar_arguments": {"event_id": "evt-2", "calendar_id": "primary"}},
    )
    follow = run_node(follow_state, _runtime())
    proposal = follow.get("calendar_action_proposal")
    assert isinstance(proposal, dict) and proposal["action_type"] == "calendar.delete_event"
    assert proposal["parameters"]["event_id"] == "evt-2"
    assert proposal["parameters"]["calendar_id"] == "primary"
    service.validate_event_write_access.assert_awaited()


def test_delete_event_from_10th_november_calendar_uses_generic_day_discovery(monkeypatch):
    tz = ZoneInfo("America/Los_Angeles")
    event = _event(
        "evt-10nov-1",
        "Planning",
        datetime(2026, 11, 10, 9, 0, tzinfo=tz),
        datetime(2026, 11, 10, 10, 0, tzinfo=tz),
        time_zone="America/Los_Angeles",
    )
    service = SimpleNamespace(
        list_events=AsyncMock(
            return_value={"events": [event], "next_page_token": None, "calendar_id": "primary"}
        ),
        get_event=AsyncMock(return_value=event),
        validate_event_write_access=AsyncMock(return_value=event),
    )
    monkeypatch.setattr(calendar_agent_module, "CalendarService", lambda _db: service)
    monkeypatch.setattr(calendar_agent_module, "SessionLocal", _SessionCtx)

    result = run_node(
        _state(
            "Delete the event from 10th november calendar.",
            timezone_name="America/Los_Angeles",
            request_time="2026-11-17T12:00:00-08:00",
        ),
        _runtime(),
    )

    assert service.list_events.await_count >= 1
    call = service.list_events.await_args_list[0]
    assert call.kwargs["user_id"] == USER_ID
    assert call.kwargs["connection_id"] == CONNECTION_ID
    assert call.kwargs["calendar_id"] == "primary"
    assert call.kwargs["query"] is None
    assert call.kwargs["time_zone"] == "America/Los_Angeles"
    assert call.kwargs["time_min"] == datetime(2026, 11, 10, 0, 0, tzinfo=tz)
    assert call.kwargs["time_max"] == datetime(2026, 11, 11, 0, 0, tzinfo=tz)

    proposal = result.get("calendar_action_proposal")
    assert isinstance(proposal, dict)
    assert proposal["action_type"] == "calendar.delete_event"
    assert proposal["parameters"]["event_id"] == "evt-10nov-1"
    service.validate_event_write_access.assert_awaited()
    resp = (result.get("assistant_response") or "").lower()
    assert "couldn’t find an existing calendar event matching the requested title" not in resp
    assert "couldn't find an existing calendar event matching the requested title" not in resp
    assert "delete" in resp or "approval" in resp
