import json
from contextlib import nullcontext
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest

from app.llm.schemas import LLMResponse
from app.workflows import calendar_agent_node
from app.workflows import gmail_agent_nodes
from app.workflows.calendar_agent_node import calendar_agent_node as run_node
from app.workflows.chat_graph import chat_graph


USER_ID = "c0cd2d85-c90c-4c62-a886-c41bec1c41af"
EVENT_CONNECTION_ID = "5a3d3426-f1fd-4cc9-ae83-08181848cde7"
FREEBUSY_CONNECTION_ID = "20f915fe-3cc3-4860-888f-aa079789c322"
UPDATE_MESSAGE = (
    "Update my existing Google Calendar event using these details:\n"
    "Event: Personal AI Assistant - Calendar Test\n"
    "Date: November 10, 2026\n"
    "Original time: 10:00 AM-10:30 AM\n"
    "New time: 11:00 AM-11:30 AM\n"
    "Timezone: Asia/Kolkata\n"
    "Calendar: Primary\n"
    "Keep the same date, title, timezone and calendar."
)


def _state(tool_name, arguments):
    return {
        "user_message": "Show my upcoming meetings.",
        "gmail_plan": {
            "intent": "google_calendar",
            "calendar_tool": tool_name,
            "calendar_arguments": arguments,
        },
        "user_timezone": "America/Los_Angeles",
        "assistant_response": "",
        "provider": "",
        "model": "",
        "messages": [],
    }


class FakeTool:
    def __init__(self, name, output):
        self.name = name
        self.output = json.dumps(output)
        self.arguments = None

    async def ainvoke(self, arguments):
        self.arguments = arguments
        return self.output


def _runtime(**context):
    return SimpleNamespace(context=context)


def _update_event(
    event_id,
    summary,
    start,
    end,
):
    return {
        "id": event_id,
        "summary": summary,
        "description": None,
        "location": None,
        "start": {
            "date_time": datetime.fromisoformat(start),
            "date": None,
            "time_zone": "Asia/Kolkata",
        },
        "end": {
            "date_time": datetime.fromisoformat(end),
            "date": None,
            "time_zone": "Asia/Kolkata",
        },
        "attendees": [],
        "etag": f'"etag-{event_id}"',
    }


def _run_mocked_calendar_update(
    monkeypatch,
    planner_arguments,
    events,
    *,
    calendar_list=None,
    user_message=UPDATE_MESSAGE,
):
    planner_response = LLMResponse(
        content=json.dumps({
            "intent": "google_calendar",
            "calendar_tool": "clarify",
            "calendar_arguments": planner_arguments,
        }),
        provider="test",
        model="test-model",
        latency_ms=0,
        finish_reason="stop",
    )
    monkeypatch.setattr(
        gmail_agent_nodes.llm_router,
        "generate",
        lambda _request: planner_response,
    )
    events_by_id = {event["id"]: event for event in events}
    service = SimpleNamespace(
        list_events=AsyncMock(return_value={"events": events}),
        validate_event_write_access=AsyncMock(
            side_effect=lambda **kwargs: events_by_id[kwargs["event_id"]]
        ),
        list_calendars=AsyncMock(return_value=calendar_list or {
            "calendars": [],
            "next_page_token": None,
        }),
    )
    monkeypatch.setattr(
        calendar_agent_node,
        "CalendarService",
        lambda _db: service,
    )
    monkeypatch.setattr(
        calendar_agent_node,
        "SessionLocal",
        lambda: nullcontext(object()),
    )
    state = {
        "user_message": user_message,
        "assistant_capabilities": {
            "google_calendar": {
                "write_permission_granted": True,
                "update_feature_enabled": True,
                "update_execution_available": True,
                "write_execution_available": True,
                "delete_execution_available": False,
            },
        },
        "messages": [],
        "user_timezone": "UTC",
    }
    routed = gmail_agent_nodes.gmail_intent_router_node(state)
    state.update(routed)
    runtime = _runtime(
        user_id=USER_ID,
        calendar_write_connection_id=EVENT_CONNECTION_ID,
        google_connection_available=True,
        calendar_enabled=True,
    )
    result = run_node(state, runtime)
    return routed, result, service, runtime


def _event(event_id, summary, start, end):
    return {
        "id": event_id,
        "summary": summary,
        "start": {"date_time": start, "date": None},
        "end": {"date_time": end, "date": None},
    }


def test_event_tool_uses_server_selected_identity_and_renders_grounded_result(
    monkeypatch,
):
    tool = FakeTool("list_calendar_events", {
        "ok": True,
        "data": {
            "time_zone": "UTC",
            "events": [{
                "id": "event-1",
                "summary": "Planning",
                "start": {
                    "date_time": "2026-11-05T17:00:00+00:00",
                    "date": None,
                },
                "end": {
                    "date_time": "2026-11-05T18:00:00+00:00",
                    "date": None,
                },
                "attendees": [{"display_name": "Alex", "email": "alex@example.com"}],
                "location": "Room 2",
                "description": "Review the launch plan.",
            }],
        },
    })
    factory = Mock(return_value=[tool])
    monkeypatch.setattr(calendar_agent_node, "create_calendar_tools", factory)

    result = run_node(
        _state("list_calendar_events", {"query": "Planning"}),
        _runtime(
            user_id=USER_ID,
            calendar_event_connection_id=EVENT_CONNECTION_ID,
            calendar_freebusy_connection_id=None,
            google_connection_available=True,
        ),
    )

    assert "Planning" in result["assistant_response"]
    assert "Alex" in result["assistant_response"]
    assert "Room 2" in result["assistant_response"]
    assert "Review the launch plan." in result["assistant_response"]
    factory.assert_called_once_with(
        user_id=USER_ID,
        connection_id=EVENT_CONNECTION_ID,
        freebusy_connection_id=None,
        include_event_tools=True,
        include_freebusy_tool=False,
    )
    assert tool.arguments == {"query": "Planning"}
    assert "access token" not in result["assistant_response"].casefold()


def test_named_calendar_is_resolved_from_provider_summary_before_event_read(
    monkeypatch,
):
    list_tool = FakeTool("list_calendars", {
        "ok": True,
        "data": {
            "calendars": [
                {
                    "id": "work-calendar-id",
                    "summary": "Work",
                    "primary": False,
                },
                {
                    "id": "primary-id",
                    "summary": "Personal",
                    "primary": True,
                },
            ],
        },
    })
    event_tool = FakeTool("list_calendar_events", {
        "ok": True,
        "data": {
            "events": [
                _event(
                    "work-event",
                    "Work planning",
                    "2026-11-06T09:00:00+00:00",
                    "2026-11-06T10:00:00+00:00",
                ),
            ],
        },
    })
    monkeypatch.setattr(
        calendar_agent_node,
        "create_calendar_tools",
        Mock(return_value=[list_tool, event_tool]),
    )
    state = _state("list_calendar_events", {
        "time_min": "2026-11-06T00:00:00Z",
        "max_results": 100,
    })
    state["user_message"] = "Show my work calendar."

    result = run_node(
        state,
        _runtime(
            user_id=USER_ID,
            calendar_event_connection_id=EVENT_CONNECTION_ID,
            calendar_freebusy_connection_id=None,
            google_connection_available=True,
        ),
    )

    assert "Work planning" in result["assistant_response"]
    assert list_tool.arguments == {}
    assert event_tool.arguments["calendar_id"] == "work-calendar-id"


def test_ambiguous_named_calendar_requests_clarification_without_event_read(
    monkeypatch,
):
    list_tool = FakeTool("list_calendars", {
        "ok": True,
        "data": {
            "calendars": [
                {"id": "work-one", "summary": "Work North"},
                {"id": "work-two", "summary": "Work South"},
            ],
        },
    })
    event_tool = FakeTool("list_calendar_events", {
        "ok": True,
        "data": {"events": []},
    })
    monkeypatch.setattr(
        calendar_agent_node,
        "create_calendar_tools",
        Mock(return_value=[list_tool, event_tool]),
    )
    state = _state("list_calendar_events", {})
    state["user_message"] = "Show my work calendar."

    result = run_node(
        state,
        _runtime(
            user_id=USER_ID,
            calendar_event_connection_id=EVENT_CONNECTION_ID,
            calendar_freebusy_connection_id=None,
            google_connection_available=True,
        ),
    )

    assert "Which one do you mean?" in result["assistant_response"]
    assert event_tool.arguments is None


def test_missing_calendar_list_scope_explains_the_connected_apps_grant(
    monkeypatch,
):
    list_tool = FakeTool("list_calendars", {
        "ok": False,
        "error": {
            "code": "calendar_list_permission_missing",
            "message": (
                "Calendar List access is required to discover calendar "
                "names. Grant Calendar List Access for this account in "
                "Connected Apps, then try again."
            ),
        },
    })
    event_tool = FakeTool("list_calendar_events", {
        "ok": True,
        "data": {"events": []},
    })
    monkeypatch.setattr(
        calendar_agent_node,
        "create_calendar_tools",
        Mock(return_value=[list_tool, event_tool]),
    )
    state = _state("list_calendar_events", {})
    state["user_message"] = "Show my work calendar."

    result = run_node(
        state,
        _runtime(
            user_id=USER_ID,
            calendar_event_connection_id=EVENT_CONNECTION_ID,
            calendar_freebusy_connection_id=None,
            google_connection_available=True,
        ),
    )

    assert "Grant Calendar List Access" in result["assistant_response"]
    assert event_tool.arguments is None


def test_listing_calendars_renders_provider_summaries_without_ids():
    response = calendar_agent_node._render_result(
        "list_calendars",
        {
            "calendars": [
                {
                    "id": "personal-id",
                    "summary": "Personal",
                    "primary": True,
                    "access_role": "owner",
                    "time_zone": "Asia/Kolkata",
                },
            ],
        },
        "Asia/Kolkata",
    )

    assert "Personal" in response
    assert "primary" in response
    assert "Asia/Kolkata" in response
    assert "personal-id" not in response


def test_list_calendars_consumes_all_continuation_pages(monkeypatch):
    class PaginatedTool:
        name = "list_calendars"

        def __init__(self):
            self.arguments = []

        async def ainvoke(self, arguments):
            self.arguments.append(arguments)
            if len(self.arguments) == 1:
                return json.dumps({
                    "ok": True,
                    "data": {
                        "calendars": [
                            {
                                "id": "primary-id",
                                "summary": "Personal",
                                "primary": True,
                            },
                        ],
                        "next_page_token": "next-page",
                    },
                })
            return json.dumps({
                "ok": True,
                "data": {
                    "calendars": [
                        {
                            "id": "work-id",
                            "summary": "Work",
                            "primary": False,
                        },
                    ],
                    "next_page_token": None,
                },
            })

    tool = PaginatedTool()
    monkeypatch.setattr(
        calendar_agent_node,
        "create_calendar_tools",
        Mock(return_value=[tool]),
    )

    result = run_node(
        _state("list_calendars", {}),
        _runtime(
            user_id=USER_ID,
            calendar_event_connection_id=EVENT_CONNECTION_ID,
            calendar_freebusy_connection_id=None,
            google_connection_available=True,
        ),
    )

    assert "Personal" in result["assistant_response"]
    assert "Work" in result["assistant_response"]
    assert tool.arguments == [{}, {"page_token": "next-page"}]


def test_next_meeting_selects_earliest_upcoming_event_across_timezone_fold(
    monkeypatch,
):
    tool = FakeTool("list_calendar_events", {
        "ok": True,
        "data": {
            "events": [
                _event(
                    "later-event",
                    "Later occurrence",
                    "2026-11-01T01:15:00-08:00",
                    "2026-11-01T02:00:00-08:00",
                ),
                _event(
                    "past-event",
                    "Past occurrence",
                    "2026-11-01T01:15:00-07:00",
                    "2026-11-01T01:25:00-07:00",
                ),
                _event(
                    "next-event",
                    "Next occurrence",
                    "2026-11-01T01:45:00-07:00",
                    "2026-11-01T02:00:00-07:00",
                ),
            ],
            "time_zone": "America/Los_Angeles",
        },
    })
    factory = Mock(return_value=[tool])
    monkeypatch.setattr(calendar_agent_node, "create_calendar_tools", factory)
    state = _state("list_calendar_events", {})
    state["user_message"] = "Show details of my next meeting."
    state["user_timezone"] = "America/Los_Angeles"

    result = run_node(
        state,
        _runtime(
            user_id=USER_ID,
            request_time="2026-11-01T08:30:00Z",
            calendar_event_connection_id=EVENT_CONNECTION_ID,
            calendar_freebusy_connection_id=None,
            google_connection_available=True,
        ),
    )

    assert "Next occurrence" in result["assistant_response"]
    assert "Later occurrence" not in result["assistant_response"]
    assert "Past occurrence" not in result["assistant_response"]
    assert tool.arguments == {
        "time_min": "2026-11-01T01:30:00-07:00",
        "time_zone": "America/Los_Angeles",
        "max_results": 10,
    }


def test_next_meeting_reports_verified_empty_upcoming_result(monkeypatch):
    tool = FakeTool("list_calendar_events", {
        "ok": True,
        "data": {
            "events": [
                _event(
                    "past-event",
                    "Past occurrence",
                    "2026-11-01T01:15:00-07:00",
                    "2026-11-01T01:25:00-07:00",
                ),
            ],
            "time_zone": "America/Los_Angeles",
        },
    })
    factory = Mock(return_value=[tool])
    monkeypatch.setattr(calendar_agent_node, "create_calendar_tools", factory)
    state = _state("list_calendar_events", {
        "time_min": "1999-01-01T00:00:00Z",
        "max_results": 100,
    })
    state["user_message"] = "Show details of my next meeting."

    result = run_node(
        state,
        _runtime(
            user_id=USER_ID,
            request_time="2026-11-01T08:30:00Z",
            calendar_event_connection_id=EVENT_CONNECTION_ID,
            calendar_freebusy_connection_id=None,
            google_connection_available=True,
        ),
    )

    assert result["assistant_response"] == (
        "There are no upcoming events on your Google Calendar."
    )
    assert tool.arguments["time_min"] == "2026-11-01T01:30:00-07:00"
    assert tool.arguments["max_results"] == 10


def test_calendar_tool_receives_authenticated_account_and_request_time(monkeypatch):
    tool = FakeTool("get_today_schedule", {
        "ok": True,
        "data": {"events": []},
    })
    factory = Mock(return_value=[tool])
    monkeypatch.setattr(calendar_agent_node, "create_calendar_tools", factory)
    request_time = "2026-11-06T18:45:00+00:00"

    run_node(
        _state("get_today_schedule", {"time_zone": "Asia/Kolkata"}),
        _runtime(
            user_id=USER_ID,
            request_time=request_time,
            calendar_event_connection_id=EVENT_CONNECTION_ID,
            calendar_freebusy_connection_id=None,
            google_connection_available=True,
        ),
    )

    factory.assert_called_once_with(
        user_id=USER_ID,
        connection_id=EVENT_CONNECTION_ID,
        freebusy_connection_id=None,
        include_event_tools=True,
        include_freebusy_tool=False,
        request_time=request_time,
    )


def test_provider_failure_and_malformed_result_never_render_history_as_events(
    monkeypatch,
):
    stale_title = "Old product launch meeting"
    failed_tool = FakeTool("list_calendar_events", {
        "ok": False,
        "error": {
            "code": "calendar_temporarily_unavailable",
            "message": "Google Calendar is temporarily unavailable.",
        },
    })
    monkeypatch.setattr(
        calendar_agent_node,
        "create_calendar_tools",
        Mock(return_value=[failed_tool]),
    )
    state = _state("list_calendar_events", {})
    state["messages"] = [
        {"role": "assistant", "content": stale_title},
    ]
    failed = run_node(
        state,
        _runtime(
            user_id=USER_ID,
            calendar_event_connection_id=EVENT_CONNECTION_ID,
            calendar_freebusy_connection_id=None,
            google_connection_available=True,
        ),
    )
    assert "temporarily unavailable" in failed["assistant_response"]
    assert stale_title not in failed["assistant_response"]

    malformed_tool = FakeTool("list_calendar_events", {
        "ok": True,
        "data": {"events": [{"summary": stale_title}]},
    })
    monkeypatch.setattr(
        calendar_agent_node,
        "create_calendar_tools",
        Mock(return_value=[malformed_tool]),
    )
    malformed = run_node(
        _state("list_calendar_events", {}),
        _runtime(
            user_id=USER_ID,
            calendar_event_connection_id=EVENT_CONNECTION_ID,
            calendar_freebusy_connection_id=None,
            google_connection_available=True,
        ),
    )
    assert "malformed event data" in malformed["assistant_response"]
    assert stale_title not in malformed["assistant_response"]


def test_freebusy_tool_uses_its_own_granted_connection(monkeypatch):
    tool = FakeTool("check_calendar_availability", {
        "ok": True,
        "data": {
            "time_min": "2026-11-05T09:00:00-08:00",
            "time_max": "2026-11-05T10:00:00-08:00",
            "time_zone": "America/Los_Angeles",
            "calendars": {"primary": {"busy": [], "errors": []}},
        },
    })
    factory = Mock(return_value=[tool])
    monkeypatch.setattr(calendar_agent_node, "create_calendar_tools", factory)

    result = run_node(
        _state(
            "check_calendar_availability",
            {
                "time_min": "2026-11-05T09:00:00-08:00",
                "time_max": "2026-11-05T10:00:00-08:00",
                "time_zone": "America/Los_Angeles",
                "calendar_ids": ["primary"],
            },
        ),
        _runtime(
            user_id=USER_ID,
            calendar_event_connection_id=EVENT_CONNECTION_ID,
            calendar_freebusy_connection_id=FREEBUSY_CONNECTION_ID,
            google_connection_available=True,
        ),
    )

    assert "primary Google Calendar appears free" in result["assistant_response"]
    factory.assert_called_once_with(
        user_id=USER_ID,
        connection_id=EVENT_CONNECTION_ID,
        freebusy_connection_id=FREEBUSY_CONNECTION_ID,
        include_event_tools=True,
        include_freebusy_tool=True,
    )


def test_partial_freebusy_data_never_claims_availability():
    response = calendar_agent_node._render_result(
        "check_calendar_availability",
        {
            "time_min": "2026-11-05T09:00:00Z",
            "time_max": "2026-11-05T10:00:00Z",
            "time_zone": "UTC",
            "calendars": {
                "primary": {
                    "busy": [],
                    "errors": [{"reason": "internal"}],
                },
            },
        },
        "UTC",
        ["primary"],
    )
    assert "couldn’t reliably determine" in response
    assert "free" not in response.casefold()


def test_freebusy_result_displays_local_date_and_times():
    response = calendar_agent_node._render_result(
        "check_calendar_availability",
        {
            "time_min": "2026-11-14T04:30:00Z",
            "time_max": "2026-11-14T06:30:00Z",
            "time_zone": "Asia/Kolkata",
            "calendars": {
                "primary": {
                    "busy": [],
                    "errors": [],
                },
            },
        },
        "Asia/Kolkata",
        ["primary"],
    )

    assert "2026-11-14 10:00 AM IST" in response
    assert "2026-11-14 12:00 PM IST" in response


def test_freebusy_missing_grant_is_explained_without_tool_invocation(monkeypatch):
    factory = Mock()
    monkeypatch.setattr(calendar_agent_node, "create_calendar_tools", factory)
    state = _state("check_calendar_availability", {})
    state["user_message"] = "Am I free tomorrow?"

    result = run_node(
        state,
        _runtime(
            user_id=USER_ID,
            calendar_event_connection_id=EVENT_CONNECTION_ID,
            calendar_freebusy_connection_id=None,
            google_connection_available=True,
        ),
    )

    assert "separate permission" in result["assistant_response"]
    factory.assert_not_called()


def test_selected_account_missing_scope_does_not_fall_back(monkeypatch):
    factory = Mock()
    monkeypatch.setattr(calendar_agent_node, "create_calendar_tools", factory)
    state = _state("check_calendar_availability", {})
    state["user_message"] = "Am I free tomorrow?"

    result = run_node(
        state,
        _runtime(
            user_id=USER_ID,
            calendar_event_connection_id=EVENT_CONNECTION_ID,
            calendar_freebusy_connection_id=None,
            google_connection_available=True,
            calendar_default_connection_selected=True,
            calendar_selected_freebusy_scope_missing=True,
        ),
    )

    assert "lacks the Calendar free/busy permission" in result["assistant_response"]
    assert "will not switch to another account" in result["assistant_response"]
    factory.assert_not_called()


def test_multiple_eligible_accounts_require_default_selection(monkeypatch):
    factory = Mock()
    monkeypatch.setattr(calendar_agent_node, "create_calendar_tools", factory)

    result = run_node(
        _state("list_calendar_events", {}),
        _runtime(
            user_id=USER_ID,
            calendar_event_connection_id=None,
            calendar_freebusy_connection_id=None,
            google_connection_available=True,
            calendar_event_selection_required=True,
        ),
    )

    assert "Choose your default Calendar account" in result["assistant_response"]
    factory.assert_not_called()


def test_disconnected_and_ungranted_event_access_are_distinguished():
    disconnected = run_node(
        _state("list_calendar_events", {}),
        _runtime(
            user_id=USER_ID,
            calendar_event_connection_id=None,
            calendar_freebusy_connection_id=None,
            google_connection_available=False,
        ),
    )
    google_without_calendar_grant = run_node(
        _state("list_calendar_events", {}),
        _runtime(
            user_id=USER_ID,
            calendar_event_connection_id=None,
            calendar_freebusy_connection_id=None,
            google_connection_available=True,
        ),
    )
    assert "not connected" in disconnected["assistant_response"]
    assert "needs Calendar event-read permission" in (
        google_without_calendar_grant["assistant_response"]
    )


def test_calendar_write_requests_are_refused_without_tool_execution(monkeypatch):
    factory = Mock()
    monkeypatch.setattr(calendar_agent_node, "create_calendar_tools", factory)
    state = _state("clarify", None)
    state["user_message"] = "Create a meeting tomorrow."

    result = run_node(
        state,
        _runtime(
            user_id=USER_ID,
            calendar_event_connection_id=EVENT_CONNECTION_ID,
            calendar_freebusy_connection_id=FREEBUSY_CONNECTION_ID,
            google_connection_available=True,
        ),
    )

    assert "What event would you like to create?" in (
        result["assistant_response"]
    )
    assert "changes are not available" not in result["assistant_response"]
    factory.assert_not_called()


def test_calendar_create_without_write_permission_is_not_prepared(
    monkeypatch,
):
    factory = Mock()
    monkeypatch.setattr(calendar_agent_node, "create_calendar_tools", factory)
    state = _state("create_calendar_event_proposal", None)
    state["user_message"] = "Create a calendar event."
    state["assistant_capabilities"] = {
        "google_calendar": {"write_execution_available": False},
    }

    result = run_node(
        state,
        _runtime(
            user_id=USER_ID,
            calendar_enabled=True,
            google_connection_available=True,
        ),
    )

    assert "write permission" not in result["assistant_response"].casefold()
    assert "nothing was created" in result["assistant_response"].casefold()
    assert "calendar_create_event_proposal" not in result
    factory.assert_not_called()


def test_calendar_create_with_missing_date_asks_before_preflight():
    state = _state("create_calendar_event_proposal", None)
    state["user_message"] = (
        "add in my calendar gym timing morning 10am to 11am"
    )
    state["user_timezone"] = "Asia/Kolkata"
    state["assistant_capabilities"] = {
        "google_calendar": {
            "write_permission_granted": True,
            "create_execution_available": False,
            "create_feature_enabled": False,
        },
    }

    result = run_node(
        state,
        _runtime(
            user_id=USER_ID,
            calendar_write_connection_id=EVENT_CONNECTION_ID,
            calendar_enabled=True,
            google_connection_available=True,
        ),
    )

    assert "What date" in result["assistant_response"]
    assert "10am–11am" in result["assistant_response"]
    assert "Asia/Kolkata" in result["assistant_response"]
    assert "calendar_create_event_proposal" not in result


def test_calendar_create_draft_asks_only_missing_end_time_and_is_retained():
    state = _state("create_calendar_event_proposal", None)
    state["user_message"] = "10:00 AM"
    state["calendar_create_draft"] = {
        "summary": "Planning",
        "event_date": "2026-11-10",
        "start_time": "10:00",
        "time_zone": "Asia/Kolkata",
        "calendar_name": "Primary Calendar",
        "calendar_id": "primary",
    }

    result = run_node(
        state,
        _runtime(
            user_id=USER_ID,
            calendar_write_connection_id=EVENT_CONNECTION_ID,
            google_connection_available=True,
        ),
    )

    assert result["assistant_response"] == "What end time should I use?"
    assert result["calendar_create_draft"] == state["calendar_create_draft"]
    assert "calendar_create_event_proposal" not in result


def test_calendar_create_resolves_named_calendar_and_preserves_full_proposal(
    monkeypatch,
):
    state = _state("create_calendar_event_proposal", None)
    state["user_message"] = "Prepare the saved proposal."
    state["calendar_create_draft"] = {
        "summary": "Planning",
        "event_date": "2026-11-10",
        "start_time": "10:00",
        "end_time": "10:30",
        "time_zone": "Asia/Kolkata",
        "calendar_name": "Work Calendar",
        "description": "Project planning",
        "location": "Room 3",
        "attendees": ["person@example.com"],
    }
    list_calendars = AsyncMock(return_value={
        "calendars": [
            {"id": "work-calendar-id", "summary": "Work Calendar"},
        ],
        "next_page_token": None,
    })
    monkeypatch.setattr(
        calendar_agent_node,
        "CalendarService",
        lambda _db: SimpleNamespace(list_calendars=list_calendars),
    )
    monkeypatch.setattr(
        calendar_agent_node,
        "SessionLocal",
        lambda: nullcontext(object()),
    )

    result = run_node(
        state,
        _runtime(
            user_id=USER_ID,
            calendar_write_connection_id=EVENT_CONNECTION_ID,
            google_connection_available=True,
        ),
    )

    list_calendars.assert_awaited_once()
    assert result["calendar_create_event_proposal"] == {
        "calendar_id": "work-calendar-id",
        "summary": "Planning",
        "description": "Project planning",
        "start": "2026-11-10T10:00:00+05:30",
        "end": "2026-11-10T10:30:00+05:30",
        "time_zone": "Asia/Kolkata",
        "location": "Room 3",
        "attendees": ["person@example.com"],
        "all_day": False,
    }
    assert result["calendar_create_draft"]["calendar_id"] == (
        "work-calendar-id"
    )


def test_calendar_create_ambiguous_named_calendar_keeps_draft_for_clarification(
    monkeypatch,
):
    state = _state("create_calendar_event_proposal", None)
    state["user_message"] = "Use Work Calendar."
    draft = {
        "summary": "Planning",
        "event_date": "2026-11-10",
        "start_time": "10:00",
        "end_time": "10:30",
        "time_zone": "Asia/Kolkata",
        "calendar_name": "Work Calendar",
    }
    state["calendar_create_draft"] = draft
    monkeypatch.setattr(
        calendar_agent_node,
        "CalendarService",
        lambda _db: SimpleNamespace(list_calendars=AsyncMock(return_value={
            "calendars": [
                {"id": "work-one", "summary": "Work Calendar"},
                {"id": "work-two", "summary": "Work Calendar"},
            ],
            "next_page_token": None,
        })),
    )
    monkeypatch.setattr(
        calendar_agent_node,
        "SessionLocal",
        lambda: nullcontext(object()),
    )

    result = run_node(
        state,
        _runtime(
            user_id=USER_ID,
            calendar_write_connection_id=EVENT_CONNECTION_ID,
            google_connection_available=True,
        ),
    )

    assert "couldn’t uniquely resolve" in result["assistant_response"]
    assert result["calendar_create_draft"]["calendar_name"] == (
        "Work Calendar"
    )
    assert "calendar_create_event_proposal" not in result


def test_calendar_update_and_delete_requests_are_refused_without_execution(
    monkeypatch,
):
    factory = Mock()
    monkeypatch.setattr(calendar_agent_node, "create_calendar_tools", factory)

    for text, tool_name in (
        ("Move my meeting to tomorrow.", "update_calendar_event_proposal"),
        ("Move my gym session to 5 PM.", "clarify"),
        ("Delete my calendar event.", "delete_calendar_event_proposal"),
    ):
        state = _state(tool_name, {})
        state["user_message"] = text
        state["assistant_capabilities"] = {
            "google_calendar": {
                "update_execution_available": False,
                "delete_execution_available": False,
            },
        }
        result = run_node(
            state,
            _runtime(
                user_id=USER_ID,
                calendar_enabled=True,
                google_connection_available=True,
            ),
        )
        assert "missing Calendar write permission" in (
            result["assistant_response"]
        )
        assert "nothing was changed" in (
            result["assistant_response"]
        )

    factory.assert_not_called()


@pytest.mark.parametrize(
    ("tool_name", "all_capabilities_enabled"),
    [
        ("update_calendar_event_proposal", False),
        ("clarify", True),
    ],
)
def test_calendar_update_proposal_preparation_preserves_approval_gate(
    monkeypatch,
    tool_name,
    all_capabilities_enabled,
):
    event = {
        "id": "event-1",
        "summary": "Gym",
        "description": None,
        "location": None,
        "start": {
            "date_time": datetime.fromisoformat("2026-06-10T10:00:00+05:30"),
            "date": None,
            "time_zone": "Asia/Kolkata",
        },
        "end": {
            "date_time": datetime.fromisoformat("2026-06-10T11:00:00+05:30"),
            "date": None,
            "time_zone": "Asia/Kolkata",
        },
        "attendees": [],
        "etag": '"etag-1"',
    }

    class SessionContext:
        def __enter__(self):
            return object()

        def __exit__(self, *_args):
            return False

    service = SimpleNamespace(
        list_events=AsyncMock(return_value={"events": [event]}),
        validate_event_write_access=AsyncMock(return_value=event),
    )
    monkeypatch.setattr(
        calendar_agent_node,
        "CalendarService",
        lambda _db: service,
    )
    monkeypatch.setattr(calendar_agent_node, "SessionLocal", SessionContext)

    state = _state(tool_name, {
        "calendar_id": "primary",
        "event_query": "gym session",
        "changes": {"start_time": "17:00"},
    })
    state["user_message"] = "Move my gym session to 5 PM."
    calendar_capabilities = {
        "write_permission_granted": True,
        "update_execution_available": all_capabilities_enabled,
        "update_feature_enabled": all_capabilities_enabled,
    }
    if all_capabilities_enabled:
        calendar_capabilities.update({
            "connected": True,
            "google_connection_available": True,
            "events_read": True,
            "freebusy": True,
            "api_available": True,
            "api_complete": True,
            "create_execution_available": True,
            "delete_execution_available": False,
            "write_execution_available": True,
            "create_feature_enabled": True,
            "delete_feature_enabled": False,
            "write_available": True,
        })
    state["assistant_capabilities"] = {
        "google_calendar": calendar_capabilities,
    }
    result = run_node(
        state,
        _runtime(
            user_id=USER_ID,
            calendar_write_connection_id=EVENT_CONNECTION_ID,
            calendar_enabled=True,
            google_connection_available=True,
        ),
    )

    assert result["calendar_action_proposal"]["action_type"] == (
        "calendar.update_event"
    )
    assert "Before → after" in result["assistant_response"]
    assert "Gym" in result["assistant_response"]
    assert result["calendar_action_proposal"]["parameters"]["changes"] == {
        "start": {
            "dateTime": "2026-06-10T17:00:00+05:30",
            "timeZone": "Asia/Kolkata",
        },
        "end": {
            "dateTime": "2026-06-10T18:00:00+05:30",
            "timeZone": "Asia/Kolkata",
        },
    }
    assert result["calendar_action_proposal"]["parameters"]["expected_etag"] == (
        '"etag-1"'
    )
    service.list_events.assert_awaited_once()
    service.validate_event_write_access.assert_awaited_once()


@pytest.mark.parametrize(
    "planner_arguments",
    [
        None,
        {"event_query": "Planning", "changes": {"location": "Room 2"}},
    ],
)
def test_planner_clarify_recovers_original_update_details_and_prepares_proposal(
    monkeypatch,
    planner_arguments,
):
    matching_event = _update_event(
        "event-match",
        "Personal AI Assistant - Calendar Test",
        "2026-11-10T10:00:00+05:30",
        "2026-11-10T10:30:00+05:30",
    )
    wrong_time = _update_event(
        "event-wrong-time",
        "Personal AI Assistant - Calendar Test",
        "2026-11-10T14:00:00+05:30",
        "2026-11-10T14:30:00+05:30",
    )
    wrong_date = _update_event(
        "event-wrong-date",
        "Personal AI Assistant - Calendar Test",
        "2026-11-11T10:00:00+05:30",
        "2026-11-11T10:30:00+05:30",
    )

    routed, result, service, runtime = _run_mocked_calendar_update(
        monkeypatch,
        planner_arguments,
        [wrong_time, wrong_date, matching_event],
    )

    assert routed["gmail_plan"]["intent"] == "google_calendar"
    assert routed["gmail_plan"]["calendar_tool"] == "clarify"
    assert result["calendar_action_proposal"]["action_type"] == (
        "calendar.update_event"
    )
    proposal = result["calendar_action_proposal"]["parameters"]
    assert proposal["calendar_id"] == "primary"
    assert proposal["event_id"] == "event-match"
    assert proposal["expected_etag"] == '"etag-event-match"'
    assert proposal["current_event"]["start"]["dateTime"] == (
        "2026-11-10T10:00:00+05:30"
    )
    assert proposal["changes"]["start"]["dateTime"] == (
        "2026-11-10T11:00:00+05:30"
    )
    assert proposal["changes"]["end"]["dateTime"] == (
        "2026-11-10T11:30:00+05:30"
    )
    if planner_arguments and planner_arguments.get("changes", {}).get("location"):
        assert proposal["changes"]["location"] == "Room 2"

    lookup = service.list_events.await_args.kwargs
    assert lookup["query"] == "Personal AI Assistant - Calendar Test"
    assert lookup["calendar_id"] == "primary"
    assert lookup["time_min"].isoformat() == "2026-11-10T00:00:00+05:30"
    assert lookup["time_max"].isoformat() == "2026-11-11T00:00:00+05:30"
    assert service.validate_event_write_access.await_args.kwargs == {
        "user_id": UUID(USER_ID),
        "connection_id": UUID(EVENT_CONNECTION_ID),
        "calendar_id": "primary",
        "event_id": "event-match",
    }
    assert runtime.context["calendar_write_connection_id"] == EVENT_CONNECTION_ID
    assert "separately execute" in result["assistant_response"]


def test_calendar_update_clarify_requests_only_missing_event_or_proposed_time(
    monkeypatch,
):
    event_missing_message = UPDATE_MESSAGE.replace(
        "Event: Personal AI Assistant - Calendar Test\n",
        "",
    )
    time_missing_message = UPDATE_MESSAGE.replace(
        "New time: 11:00 AM-11:30 AM\n",
        "",
    )

    for message, expected in (
        (event_missing_message, "Which Calendar event should I update"),
        (time_missing_message, "proposed start time"),
    ):
        routed, result, service, _runtime_context = _run_mocked_calendar_update(
            monkeypatch,
            None,
            [],
            user_message=message,
        )
        assert routed["gmail_plan"]["calendar_tool"] == "clarify"
        assert expected.casefold() in result["assistant_response"].casefold()
        service.list_events.assert_not_awaited()
        service.validate_event_write_access.assert_not_awaited()


def test_calendar_update_disambiguates_multiple_matching_events(monkeypatch):
    first_match = _update_event(
        "event-one",
        "Personal AI Assistant - Calendar Test",
        "2026-11-10T10:00:00+05:30",
        "2026-11-10T10:30:00+05:30",
    )
    second_match = _update_event(
        "event-two",
        "Personal AI Assistant - Calendar Test",
        "2026-11-10T10:00:00+05:30",
        "2026-11-10T10:30:00+05:30",
    )

    _routed, result, service, _runtime_context = _run_mocked_calendar_update(
        monkeypatch,
        None,
        [first_match, second_match],
    )

    assert "multiple possible Calendar events" in result["assistant_response"]
    assert "event-one" not in result
    service.validate_event_write_access.assert_not_awaited()


def test_calendar_update_with_no_matching_event_is_not_prepared(monkeypatch):
    wrong_date = _update_event(
        "event-wrong-date",
        "Personal AI Assistant - Calendar Test",
        "2026-11-11T10:00:00+05:30",
        "2026-11-11T10:30:00+05:30",
    )

    _routed, result, service, _runtime_context = _run_mocked_calendar_update(
        monkeypatch,
        None,
        [wrong_date],
    )

    assert "couldn’t find an existing Calendar event" in (
        result["assistant_response"]
    )
    assert "calendar_action_proposal" not in result
    service.validate_event_write_access.assert_not_awaited()


def test_calendar_update_resolves_named_calendar_using_selected_connection(
    monkeypatch,
):
    event = _update_event(
        "event-work",
        "Personal AI Assistant - Calendar Test",
        "2026-11-10T10:00:00+05:30",
        "2026-11-10T10:30:00+05:30",
    )
    message = UPDATE_MESSAGE.replace("Calendar: Primary", "Calendar: Work")

    _routed, result, service, _runtime_context = _run_mocked_calendar_update(
        monkeypatch,
        None,
        [event],
        calendar_list={
            "calendars": [{"id": "work-calendar-id", "summary": "Work"}],
            "next_page_token": None,
        },
        user_message=message,
    )

    assert result["calendar_action_proposal"]["parameters"]["calendar_id"] == (
        "work-calendar-id"
    )
    service.list_calendars.assert_awaited_once()
    assert service.list_events.await_args.kwargs["calendar_id"] == (
        "work-calendar-id"
    )


@pytest.mark.parametrize(
    ("arguments", "message", "expected", "not_expected"),
    [
        (
            {"event_query": "Planning"},
            "Update my Calendar event named Planning.",
            "proposed start time",
            "Which Calendar event",
        ),
        (
            {"changes": {"summary": "Updated Planning"}},
            "Update my Calendar event and change its title.",
            "Which Calendar event should I update",
            "What would you like to change",
        ),
        (
            {},
            "Update my Calendar event.",
            "which Calendar event to update and what",
            "changes are not available",
        ),
    ],
)
def test_clarify_update_asks_only_for_missing_details(
    monkeypatch,
    arguments,
    message,
    expected,
    not_expected,
):
    session_factory = Mock()
    monkeypatch.setattr(calendar_agent_node, "SessionLocal", session_factory)
    state = _state("clarify", arguments)
    state["user_message"] = message
    state["assistant_capabilities"] = {
        "google_calendar": {
            "write_permission_granted": True,
            "update_feature_enabled": True,
            "update_execution_available": True,
            "write_execution_available": True,
        },
    }

    result = run_node(
        state,
        _runtime(
            user_id=USER_ID,
            calendar_write_connection_id=EVENT_CONNECTION_ID,
            calendar_enabled=True,
            google_connection_available=True,
        ),
    )

    assert expected.casefold() in result["assistant_response"].casefold()
    assert not_expected.casefold() not in result["assistant_response"].casefold()
    session_factory.assert_not_called()


def test_relative_calendar_time_change_preserves_event_date_and_duration():
    event = {
        "start": {
            "date_time": datetime.fromisoformat("2026-06-10T10:00:00+05:30"),
            "time_zone": "Asia/Kolkata",
        },
        "end": {
            "date_time": datetime.fromisoformat("2026-06-10T11:00:00+05:30"),
            "time_zone": "Asia/Kolkata",
        },
    }

    changes, timezone_name = calendar_agent_node._resolve_relative_time_changes(
        event,
        {"start_time": "17:00", "duration_delta_minutes": 30},
        "UTC",
    )

    assert timezone_name == "Asia/Kolkata"
    assert changes["start"]["dateTime"] == "2026-06-10T17:00:00+05:30"
    assert changes["end"]["dateTime"] == "2026-06-10T18:30:00+05:30"


def test_calendar_write_consent_is_requested_only_for_selected_account():
    state = _state("update_calendar_event_proposal", {})
    state["assistant_capabilities"] = {
        "google_calendar": {
            "update_feature_enabled": True,
            "update_execution_available": False,
        },
    }

    result = run_node(
        state,
        _runtime(
            user_id=USER_ID,
            calendar_enabled=True,
            google_connection_available=True,
            calendar_selected_write_scope_missing=True,
        ),
    )

    assert "selected google account is missing calendar write permission" in (
        result["assistant_response"].casefold()
    )
    assert "nothing was changed" in result["assistant_response"].casefold()


def test_chat_graph_routes_approved_gym_status_question_without_provider_read(monkeypatch):
    proposal_id = uuid4()
    provider_read = AsyncMock(return_value={"events": []})
    monkeypatch.setattr(calendar_agent_node.CalendarService, "list_events", provider_read)
    result = chat_graph.invoke(
        {
            "user_id": USER_ID,
            "conversation_id": str(uuid4()),
            "user_message": "What is the status of my gym event?",
            "user_timezone": "Asia/Kolkata",
            "assistant_response": "",
            "provider": "",
            "model": "",
            "messages": [],
            "pending_calendar_create_proposal": {
                "proposal_id": str(proposal_id),
                "status": "approved",
                "parameters": {
                    "summary": "Gym",
                    "start": "2026-11-09T10:00:00+05:30",
                    "end": "2026-11-09T11:00:00+05:30",
                    "time_zone": "Asia/Kolkata",
                },
            },
            "assistant_capabilities": {
                "google_calendar": {
                    "calendar_enabled": True,
                    "create_execution_available": False,
                },
            },
        },
        config={"configurable": {"thread_id": str(uuid4())}},
        context={
            "user_id": USER_ID,
            "gmail_connection_id": None,
            "user_timezone": "Asia/Kolkata",
            "calendar_event_connection_id": EVENT_CONNECTION_ID,
            "calendar_freebusy_connection_id": None,
            "google_connection_available": True,
            "calendar_enabled": True,
        },
    )

    assert result["assistant_route"] == "google_calendar"
    assert result["gmail_plan"]["calendar_tool"] == "get_calendar_proposal_status"
    assert "Approval = Approved" in result["assistant_response"]
    assert "Execution = Disabled" in result["assistant_response"]
    assert "Execution attempted = No" in result["assistant_response"]
    assert "Provider confirmation = None" in result["assistant_response"]
    assert "couldn't determine" not in result["assistant_response"].casefold()
    provider_read.assert_not_awaited()


def test_chat_graph_routes_gym_live_verification_without_clarification(monkeypatch):
    proposal_id = uuid4()
    provider_read = AsyncMock(return_value={"events": []})
    monkeypatch.setattr(
        calendar_agent_node.CalendarService,
        "list_events",
        provider_read,
    )
    result = chat_graph.invoke(
        {
            "user_id": USER_ID,
            "conversation_id": str(uuid4()),
            "user_message": (
                "Has my gym event been created in Google Calendar? "
                "Check the actual calendar, not just the approval record."
            ),
            "user_timezone": "Asia/Kolkata",
            "assistant_response": "",
            "provider": "",
            "model": "",
            "messages": [],
            "pending_calendar_create_proposal": {
                "proposal_id": str(proposal_id),
                "status": "approved",
                "parameters": {
                    "summary": "Gym",
                    "start": "2026-11-09T10:00:00+05:30",
                    "end": "2026-11-09T11:00:00+05:30",
                    "time_zone": "Asia/Kolkata",
                },
            },
            "assistant_capabilities": {
                "google_calendar": {
                    "calendar_enabled": True,
                    "create_execution_available": False,
                },
            },
        },
        config={"configurable": {"thread_id": str(uuid4())}},
        context={
            "user_id": USER_ID,
            "gmail_connection_id": None,
            "user_timezone": "Asia/Kolkata",
            "calendar_event_connection_id": EVENT_CONNECTION_ID,
            "calendar_freebusy_connection_id": None,
            "google_connection_available": True,
            "calendar_enabled": True,
        },
    )

    assert result["assistant_route"] == "google_calendar"
    assert result["gmail_plan"]["calendar_tool"] == "verify_calendar_proposal_event"
    assert result["gmail_plan"]["calendar_arguments"]["parameters"]["summary"] == "Gym"
    provider_read.assert_awaited_once()
    provider_args = provider_read.await_args.kwargs
    assert provider_args["calendar_id"] == "primary"
    assert provider_args["query"] == "Gym"
    assert str(provider_args["time_min"].isoformat()) == "2026-11-09T10:00:00+05:30"
    assert str(provider_args["time_max"].isoformat()) == "2026-11-09T11:00:00+05:30"
    assert "No matching Gym event" in result["assistant_response"]
    assert "couldn't determine" not in result["assistant_response"].casefold()


@pytest.mark.parametrize(
    ("provider_events", "expected_text"),
    [
        (
            [{
                "summary": "Gym",
                "start": {"date_time": "2026-11-09T10:00:00+05:30", "date": None},
                "end": {"date_time": "2026-11-09T11:00:00+05:30", "date": None},
            }],
            "Event found in Google Calendar",
        ),
        (
            [{
                "summary": "Gym",
                "start": {"date_time": "2026-11-09T12:00:00+05:30", "date": None},
                "end": {"date_time": "2026-11-09T13:00:00+05:30", "date": None},
            }],
            "title, start time, or end time differs",
        ),
    ],
)
def test_calendar_proposal_verification_compares_provider_details(
    monkeypatch,
    provider_events,
    expected_text,
):
    monkeypatch.setattr(
        calendar_agent_node.CalendarService,
        "list_events",
        AsyncMock(return_value={"events": provider_events}),
    )
    state = {
        "user_id": USER_ID,
        "conversation_id": str(uuid4()),
        "user_message": "Check whether my Gym event exists in my calendar.",
        "gmail_plan": {
            "intent": "google_calendar",
            "calendar_tool": "verify_calendar_proposal_event",
            "calendar_arguments": {
                "proposal_id": str(uuid4()),
                "parameters": {
                    "summary": "Gym",
                    "start": "2026-11-09T10:00:00+05:30",
                    "end": "2026-11-09T11:00:00+05:30",
                    "time_zone": "Asia/Kolkata",
                    "calendar_id": "primary",
                },
            },
        },
        "assistant_response": "",
        "provider": "",
        "model": "",
        "messages": [],
    }
    result = run_node(
        state,
        _runtime(
            user_id=USER_ID,
            calendar_enabled=True,
            google_connection_available=True,
            calendar_event_connection_id=EVENT_CONNECTION_ID,
        ),
    )
    assert expected_text in result["assistant_response"]


def test_calendar_proposal_verification_provider_failure_is_actionable(monkeypatch):
    monkeypatch.setattr(
        calendar_agent_node.CalendarService,
        "list_events",
        AsyncMock(side_effect=RuntimeError("provider read failed")),
    )
    state = {
        "user_id": USER_ID,
        "conversation_id": str(uuid4()),
        "user_message": "Has my gym event been added?",
        "gmail_plan": {
            "intent": "google_calendar",
            "calendar_tool": "verify_calendar_proposal_event",
            "calendar_arguments": {
                "proposal_id": str(uuid4()),
                "parameters": {
                    "summary": "Gym",
                    "start": "2026-11-09T10:00:00+05:30",
                    "end": "2026-11-09T11:00:00+05:30",
                    "time_zone": "Asia/Kolkata",
                    "calendar_id": "primary",
                },
            },
        },
        "assistant_response": "",
        "provider": "",
        "model": "",
        "messages": [],
    }
    result = run_node(
        state,
        _runtime(
            user_id=USER_ID,
            calendar_enabled=True,
            google_connection_available=True,
            calendar_event_connection_id=EVENT_CONNECTION_ID,
        ),
    )
    assert "Live verification unavailable" in result["assistant_response"]
    assert "could not be queried" in result["assistant_response"]


def test_chat_graph_routes_upcoming_events_to_calendar_tool(monkeypatch):
    tool = FakeTool("list_calendar_events", {
        "ok": True,
        "data": {"events": [], "time_zone": "UTC"},
    })
    factory = Mock(return_value=[tool])
    monkeypatch.setattr(calendar_agent_node, "create_calendar_tools", factory)

    result = chat_graph.invoke(
        {
            "user_id": USER_ID,
            "conversation_id": str(uuid4()),
            "user_message": "Show my upcoming meetings.",
            "user_timezone": "UTC",
            "assistant_response": "",
            "provider": "",
            "model": "",
            "messages": [],
        },
        config={"configurable": {"thread_id": str(uuid4())}},
        context={
            "user_id": USER_ID,
            "gmail_connection_id": None,
            "user_timezone": "UTC",
            "calendar_event_connection_id": EVENT_CONNECTION_ID,
            "calendar_freebusy_connection_id": None,
            "google_connection_available": True,
        },
    )

    assert "returned no matching events" in (
        result["assistant_response"]
    )
    factory.assert_called_once()
