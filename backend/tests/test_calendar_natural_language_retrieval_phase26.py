from contextlib import nullcontext
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from app.workflows import calendar_agent_node as calendar_agent_module
from app.workflows import gmail_agent_nodes
from app.workflows.calendar_agent_node import (
    _is_calendar_delete_request,
    _recent_displayed_calendar_event,
    _title_token_phrase_matches,
    calendar_agent_node as run_calendar_node,
)
from app.workflows.calendar_update_parser import (
    NO_DATE_LOOKBACK_DAYS,
    build_calendar_search_query,
    parse_calendar_delete_request,
)

USER_ID = UUID("c0cd2d85-c90c-4c62-a886-c41bec1c41af")
CONVERSATION_ID = UUID("11111111-2222-3333-4444-555555555555")
CONNECTION_ID = UUID("5a3d3426-f1fd-4cc9-ae83-08181848cde7")
TZ_NAME = "Asia/Kolkata"
TZ = ZoneInfo(TZ_NAME)
REFERENCE_TIME = datetime(2026, 11, 14, 15, 0, tzinfo=TZ)


def _runtime(**overrides):
    context = {
        "user_id": str(USER_ID),
        "calendar_write_connection_id": str(CONNECTION_ID),
        "google_connection_available": True,
        "calendar_enabled": True,
        "contacts_connection_id": None,
        "drive_connection_id": None,
    }
    context.update(overrides)
    return SimpleNamespace(context=context)


def _state(
    message,
    *,
    request_time="2026-11-14T15:00:00+05:30",
    plan=None,
    messages=None,
    pending_calendar_read=None,
):
    state = {
        "user_message": message,
        "gmail_plan": plan
        or {
            "intent": "google_calendar",
            "calendar_tool": "delete_calendar_event_proposal",
            "calendar_arguments": None,
        },
        "user_id": str(USER_ID),
        "conversation_id": str(CONVERSATION_ID),
        "user_timezone": TZ_NAME,
        "request_time": request_time,
        "assistant_response": "",
        "provider": "",
        "model": "",
        "messages": messages or [],
        "assistant_capabilities": {
            "google_calendar": {
                "connected": True,
                "api_available": True,
                "events_read": True,
                "write_permission_granted": True,
                "delete_execution_available": True,
            }
        },
    }
    if pending_calendar_read is not None:
        state["pending_calendar_read"] = pending_calendar_read
    return state


def _event(
    event_id,
    summary,
    start,
    *,
    status="confirmed",
    etag='"etag-phase26"',
    duration_minutes=60,
    recurrence=None,
):
    end = start + timedelta(minutes=duration_minutes)
    event = {
        "id": event_id,
        "summary": summary,
        "status": status,
        "etag": etag,
        "start": {"date_time": start, "time_zone": TZ_NAME},
        "end": {"date_time": end, "time_zone": TZ_NAME},
        "attendees": [],
        "description": None,
        "location": None,
        "html_link": f"https://calendar.google.com/event?eid={event_id}",
    }
    if recurrence is not None:
        event["recurrence"] = recurrence
    return event


def _install_service(monkeypatch, events=None, *, event=None):
    lookup_event = event or (events[0] if events else None)
    service = SimpleNamespace(
        list_events=AsyncMock(
            return_value={
                "events": events or [],
                "next_page_token": None,
                "calendar_id": "primary",
            }
        ),
        get_event=AsyncMock(return_value=lookup_event),
        validate_event_write_access=AsyncMock(return_value=lookup_event),
        delete_event=AsyncMock(),
    )
    monkeypatch.setattr(
        calendar_agent_module, "CalendarService", lambda _db: service
    )
    monkeypatch.setattr(
        calendar_agent_module, "SessionLocal", lambda: nullcontext(object())
    )
    return service


def _pending_read(*, expires_at):
    return {
        "version": 1,
        "type": "calendar_read_continuation",
        "user_id": str(USER_ID),
        "conversation_id": str(CONVERSATION_ID),
        "calendar_id": "primary",
        "created_at": "2026-11-14T09:55:00+05:30",
        "expires_at": expires_at.isoformat(),
    }


def _displayed_event_context(*, expires_at, user_id=None, conversation_id=None, event_id="evt-phase26-display-gym"):
    pending = _pending_read(expires_at=expires_at)
    if user_id is not None:
        pending["user_id"] = str(user_id)
    if conversation_id is not None:
        pending["conversation_id"] = str(conversation_id)
    card = {
        "id": event_id,
        "title": "Gym time",
        "date": "2026-11-21",
        "start": "2026-11-21 08:00 AM IST",
        "end": "2026-11-21 09:00 AM IST",
        "timezone": TZ_NAME,
        "calendar_name": "primary",
        "attendees": [],
        "description": "",
        "meeting_links": [],
    }
    messages = [
        {
            "role": "assistant",
            "content": "Here is the event.",
            "metadata": {
                "pending_calendar_read": pending,
                "calendar_events": [card],
            },
        }
    ]
    return _state(
        "Remove the event I just showed you.",
        messages=messages,
        pending_calendar_read=pending,
    )


@pytest.mark.parametrize(
    "message",
    (
        "Delete the gym event for November 15, 2026.",
        "Delete Gym time for.",
        "Remove my gym session tomorrow.",
    ),
)
def test_reported_phrases_route_to_calendar(message):
    result = gmail_agent_nodes.gmail_intent_router_node(_state(message))
    assert result.get("assistant_route") == "google_calendar"
    plan = result.get("gmail_plan") or {}
    assert plan.get("intent") == "google_calendar"
    assert plan.get("calendar_tool") == "delete_calendar_event_proposal"


@pytest.mark.parametrize(
    "message",
    (
        "Delete the gym event for November 15, 2026.",
        "Delete Gym time for.",
        "Remove my gym session tomorrow.",
    ),
)
def test_calendar_delete_detector_accepts_reported_phrases(message):
    assert _is_calendar_delete_request(message) is True


def test_parser_strips_date_time_language_from_event_query():
    details = parse_calendar_delete_request(
        "Delete Gym time for November 15, 2026 from 9am to 10am.",
        planner_time_zone=TZ_NAME,
        reference_time=REFERENCE_TIME,
    )
    assert details["event_query"] == "Gym time"
    assert build_calendar_search_query(details["event_query"]) == "Gym time"
    assert details["event_date"] == date(2026, 11, 15)


def test_parser_uses_asia_kolkata_local_day_bounds():
    details = parse_calendar_delete_request(
        "Delete the gym event for November 15, 2026.",
        planner_time_zone=TZ_NAME,
        reference_time=REFERENCE_TIME,
    )
    assert details["lookup_time_min"].isoformat() == "2026-11-15T00:00:00+05:30"
    assert details["lookup_time_max"].isoformat() == "2026-11-16T00:00:00+05:30"
    assert details["lookup_time_min"].tzinfo.key == TZ_NAME
    assert details["lookup_time_max"].tzinfo.key == TZ_NAME


def test_parser_bounds_undated_title_lookup():
    details = parse_calendar_delete_request(
        "Delete Gym time for.",
        planner_time_zone=TZ_NAME,
        reference_time=REFERENCE_TIME,
    )
    expected_min = datetime.combine(
        REFERENCE_TIME.date() - timedelta(days=NO_DATE_LOOKBACK_DAYS),
        time.min,
        tzinfo=TZ,
    )
    expected_max = datetime(2026, 12, 15, 0, 0, tzinfo=TZ)
    assert details["no_date_lookup_window"] is True
    assert details["lookup_time_min"] == expected_min
    assert details["lookup_time_max"] == expected_max
    assert details["event_query"] == "Gym time"


def test_calendar_search_query_never_contains_raw_date_time_language():
    query = build_calendar_search_query("Gym time tomorrow 9am to 10am")
    assert query == "Gym time"
    assert "tomorrow" not in (query or "").casefold()
    assert "9am" not in (query or "").casefold()


def test_partial_title_matching_is_token_bounded():
    assert _title_token_phrase_matches("Gym time for", "gym") is True
    assert _title_token_phrase_matches("Gym session", "gym") is True
    assert _title_token_phrase_matches("Gymnastics", "gym") is False
    assert _title_token_phrase_matches("Team meeting", "gym") is False


def test_reported_november_15_phrase_finds_partial_title(monkeypatch):
    event = _event(
        "evt-phase26-nov15-gymtime",
        "Gym time for",
        datetime(2026, 11, 15, 9, 0, tzinfo=TZ),
    )
    service = _install_service(monkeypatch, [event])

    result = run_calendar_node(
        _state("Delete the gym event for November 15, 2026."), _runtime()
    )

    call = service.list_events.await_args_list[0]
    assert call.kwargs["query"] == "gym"
    assert call.kwargs["time_min"] == datetime(2026, 11, 15, 0, 0, tzinfo=TZ)
    assert call.kwargs["time_max"] == datetime(2026, 11, 16, 0, 0, tzinfo=TZ)
    proposal = result.get("calendar_action_proposal")
    assert isinstance(proposal, dict)
    assert proposal["parameters"]["event_id"] == "evt-phase26-nov15-gymtime"
    assert service.delete_event.await_count == 0


def test_reported_november_15_phrase_requests_one_focused_clarification(monkeypatch):
    first = _event(
        "evt-phase26-nov15-candidate-a",
        "Gym time for",
        datetime(2026, 11, 15, 9, 0, tzinfo=TZ),
    )
    second = _event(
        "evt-phase26-nov15-candidate-b",
        "Gym session",
        datetime(2026, 11, 15, 18, 0, tzinfo=TZ),
    )
    service = _install_service(monkeypatch, [first, second])

    result = run_calendar_node(
        _state("Delete the gym event for November 15, 2026."), _runtime()
    )

    assert "calendar_action_proposal" not in result
    response = (result.get("assistant_response") or "").casefold()
    assert "multiple" in response or "which one" in response
    assert "Gym time for" in result["assistant_response"]
    assert "Gym session" in result["assistant_response"]
    selection = result.get("pending_calendar_delete_selection")
    assert isinstance(selection, dict)
    assert [item["event_id"] for item in selection["candidates"]] == [
        "evt-phase26-nov15-candidate-a",
        "evt-phase26-nov15-candidate-b",
    ]
    assert selection["user_id"] == str(USER_ID)
    assert selection["conversation_id"] == str(CONVERSATION_ID)
    service.validate_event_write_access.assert_not_awaited()
    service.delete_event.assert_not_awaited()


def test_reported_bare_title_finds_single_event(monkeypatch):
    event = _event(
        "evt-phase26-bare-gymtime",
        "Gym time for",
        datetime(2026, 11, 15, 9, 0, tzinfo=TZ),
    )
    service = _install_service(monkeypatch, [event])

    result = run_calendar_node(_state("Delete Gym time for."), _runtime())

    assert service.list_events.await_args_list[0].kwargs["query"] == "Gym time"
    proposal = result.get("calendar_action_proposal")
    assert isinstance(proposal, dict)
    assert proposal["parameters"]["event_id"] == "evt-phase26-bare-gymtime"
    assert service.delete_event.await_count == 0


def test_reported_tomorrow_session_phrase_uses_local_day_bounds(monkeypatch):
    event = _event(
        "evt-phase26-tomorrow-session",
        "Gym session",
        datetime(2026, 11, 15, 18, 0, tzinfo=TZ),
    )
    service = _install_service(monkeypatch, [event])

    result = run_calendar_node(
        _state("Remove my gym session tomorrow."), _runtime()
    )

    call = service.list_events.await_args_list[0]
    assert call.kwargs["query"] == "gym"
    assert call.kwargs["time_min"] == datetime(2026, 11, 15, 0, 0, tzinfo=TZ)
    assert call.kwargs["time_max"] == datetime(2026, 11, 16, 0, 0, tzinfo=TZ)
    proposal = result.get("calendar_action_proposal")
    assert isinstance(proposal, dict)
    assert proposal["parameters"]["event_id"] == "evt-phase26-tomorrow-session"


def test_cancelled_events_are_not_candidates(monkeypatch):
    cancelled = _event(
        "evt-phase26-cancelled-gym",
        "Gym time for",
        datetime(2026, 11, 15, 9, 0, tzinfo=TZ),
        status="cancelled",
    )
    confirmed = _event(
        "evt-phase26-confirmed-gym",
        "Gym time for",
        datetime(2026, 11, 15, 16, 0, tzinfo=TZ),
    )
    service = _install_service(monkeypatch, [cancelled, confirmed])

    result = run_calendar_node(
        _state("Delete the gym event for November 15, 2026."), _runtime()
    )

    proposal = result.get("calendar_action_proposal")
    assert isinstance(proposal, dict)
    assert proposal["parameters"]["event_id"] == "evt-phase26-confirmed-gym"


def test_earlier_same_day_event_is_not_hidden_by_default_now(monkeypatch):
    earlier = _event(
        "evt-phase26-earlier-gym",
        "Gym time for",
        datetime(2026, 11, 14, 9, 0, tzinfo=TZ),
    )
    service = _install_service(monkeypatch, [earlier])

    result = run_calendar_node(_state("Delete Gym time for."), _runtime())

    assert service.list_events.await_args_list[0].kwargs["time_min"] == datetime(
        2026, 11, 13, 0, 0, tzinfo=TZ
    )
    proposal = result.get("calendar_action_proposal")
    assert isinstance(proposal, dict)
    assert proposal["parameters"]["event_id"] == "evt-phase26-earlier-gym"


def test_generic_day_discovery_uses_no_title_query(monkeypatch):
    event = _event(
        "evt-phase26-generic-nov10",
        "Planning",
        datetime(2026, 11, 10, 9, 0, tzinfo=TZ),
    )
    service = _install_service(monkeypatch, [event])

    result = run_calendar_node(
        _state(
            "Delete the event from 10th november calendar.",
            request_time="2026-11-17T12:00:00+05:30",
        ),
        _runtime(),
    )

    assert service.list_events.await_args_list[0].kwargs["query"] is None
    proposal = result.get("calendar_action_proposal")
    assert isinstance(proposal, dict)
    assert proposal["parameters"]["event_id"] == "evt-phase26-generic-nov10"


def test_displayed_event_context_requires_scoped_unexpired_metadata():
    valid = _displayed_event_context(
        expires_at=datetime(2026, 11, 14, 15, 5, tzinfo=TZ)
    )
    assert _recent_displayed_calendar_event(valid)["id"] == (
        "evt-phase26-display-gym"
    )

    other_user = _displayed_event_context(
        expires_at=datetime(2026, 11, 14, 15, 5, tzinfo=TZ),
        user_id=UUID("33333333-3333-3333-3333-333333333333"),
    )
    assert _recent_displayed_calendar_event(other_user) is None

    expired = _displayed_event_context(
        expires_at=datetime(2026, 11, 14, 14, 59, tzinfo=TZ)
    )
    assert _recent_displayed_calendar_event(expired) is None


def test_displayed_reference_uses_authoritative_read_metadata(monkeypatch):
    event = _event(
        "evt-phase26-display-gym",
        "Gym time",
        datetime(2026, 11, 21, 8, 0, tzinfo=TZ),
        etag='"etag-display-phase26"',
    )
    service = _install_service(monkeypatch, [event], event=event)
    state = _displayed_event_context(
        expires_at=datetime(2026, 11, 14, 15, 5, tzinfo=TZ)
    )

    result = run_calendar_node(state, _runtime())

    proposal = result.get("calendar_action_proposal")
    assert isinstance(proposal, dict)
    assert proposal["parameters"]["event_id"] == "evt-phase26-display-gym"
    assert proposal["parameters"]["calendar_id"] == "primary"
    service.list_events.assert_not_awaited()
    service.delete_event.assert_not_awaited()


def test_planner_supplied_event_id_without_trusted_context_is_ignored(monkeypatch):
    service = _install_service(monkeypatch, [])

    result = run_calendar_node(
        _state(
            "Delete the event from my calendar.",
            plan={
                "intent": "google_calendar",
                "calendar_tool": "delete_calendar_event_proposal",
                "calendar_arguments": {
                    "event_id": "evt-phase26-llm-only-fake",
                    "calendar_id": "primary",
                },
            },
        ),
        _runtime(),
    )

    assert "calendar_action_proposal" not in result
    assert "evt-phase26-llm-only-fake" not in (
        result.get("assistant_response") or ""
    )
    service.validate_event_write_access.assert_not_awaited()
    service.delete_event.assert_not_awaited()


def test_future_only_recurrence_scope_is_rejected_before_search():
    details = parse_calendar_delete_request(
        "Delete all future occurrences of Gym.",
        planner_time_zone=TZ_NAME,
        reference_time=REFERENCE_TIME,
    )
    assert details["delete_future_occurrences_only"] is True
    assert details["delete_entire_series"] is False


def test_future_only_recurrence_request_never_searches_or_proposes(monkeypatch):
    service = _install_service(monkeypatch, [])

    result = run_calendar_node(
        _state("Delete all future occurrences of Gym."), _runtime()
    )

    assert "calendar_action_proposal" not in result
    response = (result.get("assistant_response") or "").casefold()
    assert "future occurrences" in response
    assert "nothing was changed" in response
    service.list_events.assert_not_awaited()
    service.delete_event.assert_not_awaited()


def test_explicit_series_scope_is_distinct_from_future_only_scope():
    future = parse_calendar_delete_request(
        "Delete all future occurrences of Gym.",
        planner_time_zone=TZ_NAME,
        reference_time=REFERENCE_TIME,
    )
    series = parse_calendar_delete_request(
        "Delete the entire recurring Gym series from my primary Google Calendar.",
        planner_time_zone=TZ_NAME,
        reference_time=REFERENCE_TIME,
    )
    assert future["delete_entire_series"] is False
    assert series["delete_entire_series"] is True
    assert series["delete_future_occurrences_only"] is False


def test_approval_text_is_enriched_and_no_deletion_is_executed(monkeypatch):
    event = _event(
        "evt-phase26-approval-details",
        "Gym time for",
        datetime(2026, 11, 15, 9, 0, tzinfo=TZ),
        etag='"etag-approval-phase26"',
    )
    service = _install_service(monkeypatch, [event], event=event)

    result = run_calendar_node(_state("Delete Gym time for."), _runtime())

    response = result.get("assistant_response") or ""
    assert "Title: Gym time for" in response
    assert "Calendar: primary" in response
    assert "Scope: single event" in response
    assert "Date: 2026-11-15" in response
    assert "Start:" in response
    assert "End:" in response
    assert f"Time zone: {TZ_NAME}" in response
    proposal = result["calendar_action_proposal"]
    assert proposal["parameters"]["expected_etag"] == '"etag-approval-phase26"'
    assert "calendar_execution_result" not in result
    service.delete_event.assert_not_awaited()


def test_partial_title_matching_does_not_capture_unrelated_event(monkeypatch):
    unrelated = _event(
        "evt-phase26-unrelated-team",
        "Team Building",
        datetime(2026, 11, 15, 9, 0, tzinfo=TZ),
    )
    service = _install_service(monkeypatch, [unrelated])

    result = run_calendar_node(
        _state("Delete the gym event for November 15, 2026."), _runtime()
    )

    assert "calendar_action_proposal" not in result
    assert "couldn" in (result.get("assistant_response") or "").casefold()
    service.validate_event_write_access.assert_not_awaited()
    service.delete_event.assert_not_awaited()


def test_calendar_api_call_is_scoped_to_user_and_connection(monkeypatch):
    event = _event(
        "evt-phase26-tenant-scope",
        "Gym time for",
        datetime(2026, 11, 15, 9, 0, tzinfo=TZ),
    )
    service = _install_service(monkeypatch, [event])

    run_calendar_node(
        _state("Delete Gym time for."), _runtime()
    )

    call = service.list_events.await_args_list[0]
    assert call.kwargs["user_id"] == USER_ID
    assert call.kwargs["connection_id"] == CONNECTION_ID
    assert call.kwargs["calendar_id"] == "primary"
    assert call.kwargs["time_zone"] == TZ_NAME


def test_gym_time_phrase_selects_correct_event_among_same_day_gym_events(monkeypatch):
    gym_time = _event(
        "evt-phase26-gym-time-for",
        "Gym time for",
        datetime(2026, 11, 15, 9, 0, tzinfo=TZ),
        etag='"etag-phase26-gym-time-for"',
    )
    gym_session = _event(
        "evt-phase26-gym-session",
        "Gym session",
        datetime(2026, 11, 15, 18, 0, tzinfo=TZ),
        etag='"etag-phase26-gym-session"',
    )
    service = _install_service(
        monkeypatch, [gym_time, gym_session], event=gym_time
    )
    message = "Delete the gym time event from 15th November."

    details = parse_calendar_delete_request(
        message,
        planner_time_zone=TZ_NAME,
        reference_time=REFERENCE_TIME,
    )
    assert details["event_query"] == "gym time"
    assert details["event_date"] == date(2026, 11, 15)
    assert details["time_zone"] == TZ_NAME

    result = run_calendar_node(_state(message), _runtime())

    call = service.list_events.await_args_list[0]
    assert call.kwargs["query"] == "gym time"
    assert call.kwargs["time_min"] == datetime(2026, 11, 15, 0, 0, tzinfo=TZ)
    assert call.kwargs["time_max"] == datetime(2026, 11, 16, 0, 0, tzinfo=TZ)
    assert call.kwargs["time_zone"] == TZ_NAME

    assert result.get("pending_calendar_delete_selection") is None
    proposal = result.get("calendar_action_proposal")
    assert isinstance(proposal, dict)
    assert proposal["action_type"] == "calendar.delete_event"
    params = proposal["parameters"]
    assert params["calendar_id"] == "primary"
    assert params["event_id"] == "evt-phase26-gym-time-for"
    assert params["expected_etag"] == '"etag-phase26-gym-time-for"'
    snapshot = params["current_event"]
    assert snapshot["summary"] == "Gym time for"
    assert snapshot["start"] == {
        "dateTime": "2026-11-15T09:00:00+05:30",
        "timeZone": TZ_NAME,
    }
    assert snapshot["end"] == {
        "dateTime": "2026-11-15T10:00:00+05:30",
        "timeZone": TZ_NAME,
    }

    response = result.get("assistant_response") or ""
    assert "Title: Gym time for" in response
    assert "Calendar: primary" in response
    assert "Date: 2026-11-15" in response
    assert "09:00 AM" in response
    assert "10:00 AM" in response
    assert f"Time zone: {TZ_NAME}" in response
    assert "Gym session" not in response
    assert "which one" not in response.casefold()
    assert "calendar_execution_result" not in result
    service.delete_event.assert_not_awaited()


def test_gym_only_phrase_still_clarifies_when_multiple_candidates_match(monkeypatch):
    gym_time = _event(
        "evt-phase26-clarify-gym-time-for",
        "Gym time for",
        datetime(2026, 11, 15, 9, 0, tzinfo=TZ),
    )
    gym_session = _event(
        "evt-phase26-clarify-gym-session",
        "Gym session",
        datetime(2026, 11, 15, 18, 0, tzinfo=TZ),
    )
    service = _install_service(monkeypatch, [gym_time, gym_session])

    result = run_calendar_node(
        _state("Delete the gym event from November 15"), _runtime()
    )

    assert "calendar_action_proposal" not in result
    response = (result.get("assistant_response") or "").casefold()
    assert "multiple" in response or "which one" in response
    full_response = result.get("assistant_response") or ""
    assert "Gym time for" in full_response
    assert "Gym session" in full_response
    assert "09:00 AM" in full_response
    assert "06:00 PM" in full_response

    selection = result.get("pending_calendar_delete_selection")
    assert isinstance(selection, dict)
    assert selection["operation"] == "delete_selection"
    assert selection["user_id"] == str(USER_ID)
    assert selection["conversation_id"] == str(CONVERSATION_ID)
    assert selection["calendar_id"] == "primary"
    assert [item["event_id"] for item in selection["candidates"]] == [
        "evt-phase26-clarify-gym-time-for",
        "evt-phase26-clarify-gym-session",
    ]
    service.validate_event_write_access.assert_not_awaited()
    service.delete_event.assert_not_awaited()
