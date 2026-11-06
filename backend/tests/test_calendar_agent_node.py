import json
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from app.workflows import calendar_agent_node
from app.workflows.calendar_agent_node import calendar_agent_node as run_node
from app.workflows.chat_graph import chat_graph


USER_ID = "c0cd2d85-c90c-4c62-a886-c41bec1c41af"
EVENT_CONNECTION_ID = "5a3d3426-f1fd-4cc9-ae83-08181848cde7"
FREEBUSY_CONNECTION_ID = "20f915fe-3cc3-4860-888f-aa079789c322"


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
    factory.assert_called_once_with(
        user_id=USER_ID,
        connection_id=EVENT_CONNECTION_ID,
        freebusy_connection_id=None,
        include_event_tools=True,
        include_freebusy_tool=False,
    )
    assert tool.arguments == {"query": "Planning"}
    assert "access token" not in result["assistant_response"].casefold()


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

    assert "read your Google Calendar" in result["assistant_response"]
    assert "not created or modified" in result["assistant_response"]
    factory.assert_not_called()


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
