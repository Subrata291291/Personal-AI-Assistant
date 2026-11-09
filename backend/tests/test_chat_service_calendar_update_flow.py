import json
from contextlib import nullcontext
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

from app.llm.schemas import LLMResponse
from app.services import chat_service as chat_service_module
from app.services.chat_service import ChatService
from app.workflows import calendar_agent_node
from app.workflows import calendar_update_parser
from app.workflows import gmail_agent_nodes


USER_ID = UUID("c0cd2d85-c90c-4c62-a886-c41bec1c41af")
CONVERSATION_ID = UUID("c7557e52-cd75-4ea6-9130-e927176518a8")
CONNECTION_ID = UUID("5a3d3426-f1fd-4cc9-ae83-08181848cde7")
UPDATE_REQUEST = (
    "Update my existing Google Calendar event using these details:\n"
    "Event: Personal AI Assistant - Calendar Test\n"
    "Date: November 10, 2026\n"
    "Original time: 10:00 AM-10:30 AM\n"
    "Proposed time: 11:00 AM-11:30 AM\n"
    "Timezone: Asia/Kolkata\n"
    "Calendar: Primary\n"
    "Keep the same date, title, timezone and calendar."
)
EVENT = {
    "id": "event-match",
    "summary": "Personal AI Assistant - Calendar Test",
    "description": None,
    "location": None,
    "start": {
        "date_time": datetime.fromisoformat("2026-11-10T10:00:00+05:30"),
        "date": None,
        "time_zone": "Asia/Kolkata",
    },
    "end": {
        "date_time": datetime.fromisoformat("2026-11-10T10:30:00+05:30"),
        "date": None,
        "time_zone": "Asia/Kolkata",
    },
    "attendees": [],
    "etag": '"etag-event-match"',
}


def _event_at(event_id, start, end):
    return {
        **EVENT,
        "id": event_id,
        "etag": f'"etag-{event_id}"',
        "start": {
            **EVENT["start"],
            "date_time": datetime.fromisoformat(start),
        },
        "end": {
            **EVENT["end"],
            "date_time": datetime.fromisoformat(end),
        },
    }


class FakeRequestRepository:
    def __init__(self):
        self.request_id = uuid4()

    def create_request(self, **_kwargs):
        return {"id": self.request_id, "status": "processing"}, True

    def claim_request(self, **_kwargs):
        return {"id": self.request_id}

    def attach_user_message(self, **_kwargs):
        return {"attached": True}

    def mark_execution_started(self, **_kwargs):
        return {"started": True}

    def update_status(self, **_kwargs):
        return {"updated": True}


class FakeMessageService:
    def __init__(self):
        self.messages = []

    def verify_conversation_ownership(self, **_kwargs):
        return True

    def get_request_message(self, *, role, **_kwargs):
        return next(
            (message for message in self.messages if message["role"] == role),
            None,
        )

    def create_request_message(
        self,
        *,
        conversation_id,
        role,
        content,
        metadata=None,
        **_kwargs,
    ):
        message = {
            "id": uuid4(),
            "conversation_id": conversation_id,
            "role": role,
            "content": content,
            "metadata": metadata or {},
        }
        self.messages.append(message)
        return message

    def get_conversation_messages(self, **_kwargs):
        return list(self.messages)


class FakeUserConnectionService:
    db = object()

    def get_user_calendar_access(self, **_kwargs):
        return {
            "google_connection_available": True,
            "capabilities": {
                "write_permission_granted": True,
                "calendar_write_execution_available": False,
                "calendar_update_execution_available": False,
                "calendar_create_execution_available": False,
                "calendar_delete_execution_available": False,
                "calendar_update_feature_enabled": False,
            },
            "event_read_connection_id": str(CONNECTION_ID),
            "freebusy_connection_id": None,
            "write_connection_id": str(CONNECTION_ID),
            "default_connection_invalid": False,
            "default_connection_selected": True,
            "selected_connection_email": None,
            "event_selection_required": False,
            "freebusy_selection_required": False,
            "selected_event_scope_missing": False,
            "selected_freebusy_scope_missing": False,
            "calendar_write_selection_required": False,
            "calendar_selected_write_scope_missing": False,
        }


def _make_service():
    message_service = FakeMessageService()
    request_repository = FakeRequestRepository()
    user_connection_service = FakeUserConnectionService()
    service = ChatService(
        message_service=message_service,
        user_settings_service=SimpleNamespace(
            get_settings=Mock(return_value=SimpleNamespace(memory_enabled=False))
        ),
        memory_pipeline=SimpleNamespace(process=Mock(return_value=None)),
        request_repository=request_repository,
        user_connection_service=user_connection_service,
    )
    return service, message_service


def _run_chat_service_update(monkeypatch, *, message=UPDATE_REQUEST):
    correlation_id = str(uuid4())
    planner_response = LLMResponse(
        content=json.dumps({
            "intent": "google_calendar",
            "calendar_tool": "clarify",
            "calendar_arguments": None,
        }),
        provider="test",
        model="test-model",
        latency_ms=0,
        finish_reason="stop",
    )
    planner_requests = []
    monkeypatch.setattr(
        gmail_agent_nodes.llm_router,
        "generate",
        lambda request: planner_requests.append(request) or planner_response,
    )

    calendar_service = SimpleNamespace(
        list_events=AsyncMock(return_value={"events": [
            _event_at(
                "event-wrong-time",
                "2026-11-10T14:00:00+05:30",
                "2026-11-10T14:30:00+05:30",
            ),
            _event_at(
                "event-wrong-date",
                "2026-11-11T10:00:00+05:30",
                "2026-11-11T10:30:00+05:30",
            ),
            EVENT,
        ]}),
        validate_event_write_access=AsyncMock(return_value=EVENT),
        list_calendars=AsyncMock(return_value={
            "calendars": [],
            "next_page_token": None,
        }),
    )
    monkeypatch.setattr(
        calendar_agent_node,
        "CalendarService",
        lambda _db: calendar_service,
    )
    monkeypatch.setattr(
        calendar_agent_node,
        "SessionLocal",
        lambda: nullcontext(object()),
    )

    parsed_messages = []
    original_parser = calendar_update_parser.parse_calendar_update_message

    def parse_spy(text, planner_time_zone=None):
        parsed_messages.append(text)
        return original_parser(text, planner_time_zone)

    monkeypatch.setattr(
        calendar_update_parser,
        "parse_calendar_update_message",
        parse_spy,
    )

    original_invoke = chat_service_module.chat_graph.invoke
    graph_calls = []

    def invoke_spy(state, *args, **kwargs):
        result = original_invoke(state, *args, **kwargs)
        graph_calls.append((state, result))
        return result

    monkeypatch.setattr(
        chat_service_module.chat_graph,
        "invoke",
        invoke_spy,
    )

    saved_proposals = []

    class FakeActionApprovalService:
        def __init__(self, _db):
            pass

        def create_proposal(self, *, proposal_id, payload, **_kwargs):
            saved_proposals.append((proposal_id, payload))
            return SimpleNamespace(
                model_dump=lambda mode="python": {
                    "proposal_id": str(proposal_id),
                    "action_type": payload.action_type,
                    "status": "pending",
                }
            )

    monkeypatch.setattr(
        chat_service_module,
        "ActionApprovalService",
        FakeActionApprovalService,
    )
    monkeypatch.setattr(
        ChatService,
        "_start_heartbeat",
        lambda _self, *_args, **_kwargs: (None, None, None),
    )
    monkeypatch.setattr(
        ChatService,
        "_stop_heartbeat",
        lambda _self, **_kwargs: None,
    )

    service, message_service = _make_service()
    response = service.send_message(
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        content=message,
        idempotency_key=uuid4(),
        user_timezone="UTC",
        default_calendar_connection_id=str(CONNECTION_ID),
        calendar_enabled=True,
    )
    diagnostics = {
        "correlation_id": correlation_id,
        "message_length": len(message),
        "timezone_present": "Asia/Kolkata" in message,
        "extracted_timezone": None,
        "planner_tool": "clarify",
        "planner_arguments_null": True,
        "selected_route": None,
        "message_transformed": None,
        "planner_input_contains_timezone": None,
    }
    if parsed_messages:
        diagnostics["extracted_timezone"] = (
            original_parser(parsed_messages[-1]).get("time_zone")
        )
        diagnostics["message_transformed"] = (
            parsed_messages[-1] != message.strip()
        )
    if planner_requests:
        diagnostics["planner_input_contains_timezone"] = (
            "Asia/Kolkata"
            in planner_requests[-1].messages[-1].content
        )
    if graph_calls:
        diagnostics["selected_route"] = graph_calls[-1][1].get(
            "assistant_route"
        )
        diagnostics["proposal_present_in_graph_result"] = (
            "calendar_action_proposal" in graph_calls[-1][1]
        )
    return {
        "response": response,
        "diagnostics": diagnostics,
        "calendar_service": calendar_service,
        "parsed_messages": parsed_messages,
        "planner_requests": planner_requests,
        "graph_calls": graph_calls,
        "saved_proposals": saved_proposals,
        "message_service": message_service,
        "planner_response": planner_response,
    }


def test_chat_service_langgraph_calendar_update_prepares_saved_proposal(
    monkeypatch,
):
    run = _run_chat_service_update(monkeypatch)
    diagnostic = run["diagnostics"]
    user_message = run["response"]["user_message"]
    assistant_message = run["response"]["assistant_message"]
    initial_state, graph_result = run["graph_calls"][0]

    assert user_message["content"] == UPDATE_REQUEST, diagnostic
    assert initial_state["user_message"] == UPDATE_REQUEST, diagnostic
    assert graph_result["assistant_route"] == "google_calendar", diagnostic
    assert graph_result["gmail_plan"]["calendar_tool"] == "clarify", diagnostic
    assert graph_result["gmail_plan"]["calendar_arguments"] is None, diagnostic
    assert "calendar_action_proposal" in graph_result, diagnostic
    assert run["parsed_messages"] == [UPDATE_REQUEST], diagnostic
    assert diagnostic["extracted_timezone"] == "Asia/Kolkata", diagnostic
    assert not diagnostic["message_transformed"], diagnostic
    assert diagnostic["planner_input_contains_timezone"], diagnostic
    assert run["planner_requests"][0].messages[-1].content == UPDATE_REQUEST

    lookup = run["calendar_service"].list_events.await_args.kwargs
    assert lookup["query"] == "Personal AI Assistant - Calendar Test", diagnostic
    assert lookup["calendar_id"] == "primary", diagnostic
    assert lookup["time_min"].isoformat() == (
        "2026-11-10T00:00:00+05:30"
    ), diagnostic
    assert lookup["time_max"].isoformat() == (
        "2026-11-11T00:00:00+05:30"
    ), diagnostic
    assert lookup["time_zone"] == "Asia/Kolkata", diagnostic

    assert run["calendar_service"].validate_event_write_access.await_args.kwargs == {
        "user_id": USER_ID,
        "connection_id": CONNECTION_ID,
        "calendar_id": "primary",
        "event_id": "event-match",
    }, diagnostic
    assert len(run["saved_proposals"]) == 1, diagnostic
    _proposal_id, payload = run["saved_proposals"][0]
    assert payload.action_type == "calendar.update_event", diagnostic
    assert payload.connection_id == CONNECTION_ID, diagnostic
    assert payload.parameters["expected_etag"] == '"etag-event-match"', diagnostic
    assert payload.parameters["changes"]["start"]["dateTime"] == (
        "2026-11-10T11:00:00+05:30"
    ), diagnostic
    assert payload.parameters["changes"]["end"]["dateTime"] == (
        "2026-11-10T11:30:00+05:30"
    ), diagnostic
    assert "approve and separately execute" in graph_result["assistant_response"]
    assert "action_approval" in assistant_message["metadata"]
    assert not hasattr(run["calendar_service"], "execute")
    assert run["planner_response"].content == json.dumps({
        "intent": "google_calendar",
        "calendar_tool": "clarify",
        "calendar_arguments": None,
    })
    assert run["planner_requests"]


def test_chat_service_langgraph_calendar_update_without_timezone_fails_safely(
    monkeypatch,
):
    message = UPDATE_REQUEST.replace("Timezone: Asia/Kolkata\n", "")
    run = _run_chat_service_update(monkeypatch, message=message)
    diagnostic = run["diagnostics"]
    graph_result = run["graph_calls"][0][1]

    assert graph_result["assistant_route"] == "google_calendar", diagnostic
    assert run["parsed_messages"] == [message], diagnostic
    assert "An explicit IANA timezone is required to match the event date." in (
        graph_result["assistant_response"]
    ), diagnostic
    assert run["calendar_service"].list_events.await_count == 0, diagnostic
    assert run["calendar_service"].validate_event_write_access.await_count == 0
    assert run["saved_proposals"] == []
