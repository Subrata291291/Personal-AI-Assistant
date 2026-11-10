import json
import logging
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest

from app.llm.schemas import LLMResponse
from app.core.request_metrics import RequestMetrics, current_request_metrics
from app.schemas.chat import ChatResponse
from app.services import chat_service as chat_service_module
from app.services.chat_service import ChatService
from app.workflows import calendar_agent_node
from app.workflows import calendar_update_parser
from app.workflows import gmail_agent_nodes


USER_ID = UUID("c0cd2d85-c90c-4c62-a886-c41bec1c41af")
CONVERSATION_ID = UUID("c7557e52-cd75-4ea6-9130-e927176518a8")
CONNECTION_ID = UUID("5a3d3426-f1fd-4cc9-ae83-08181848cde7")
RECURRING_CREATE_REQUEST = (
    "Create a recurring Google Calendar event with these details:\n"
    "Title: Gym\n"
    "Frequency: DAILY\n"
    "Date: November 11, 2026\n"
    "Start time: 10:00 AM\n"
    "End time: 11:00 AM\n"
    "Timezone: Asia/Kolkata\n"
    "Calendar: primary\n"
    "No end date.\n"
    "Save the proposal and wait for explicit approval and separate Execute."
)
RECURRING_CREATE_PARAMETERS = {
    "calendar_id": "primary",
    "summary": "Gym",
    "description": "Workout session",
    "start": "2026-11-11T10:00:00+05:30",
    "end": "2026-11-11T11:00:00+05:30",
    "time_zone": "Asia/Kolkata",
    "location": None,
    "attendees": [],
    "all_day": False,
    "recurrence": ["RRULE:FREQ=DAILY"],
}
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
NATURAL_TITLE_UPDATE_REQUEST = (
    "Update the existing Google Calendar event whose title starts with "
    "'A recurring google' and whose time is 10:00 AM–11:00 AM on "
    "November 10, 2026. Change only its title to Gym. Preserve the date, "
    "start time, end time, timezone, calendar, and recurrence. Prepare a "
    "saved Update proposal and wait for explicit approval and separate "
    "Execute."
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
        self.request_id = None

    def create_request(self, **_kwargs):
        self.request_id = uuid4()
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

    def get_request_message(self, *, role, chat_request_id=None, **_kwargs):
        return next(
            (
                message for message in self.messages
                if message["role"] == role
                and (
                    chat_request_id is None
                    or message.get("chat_request_id") == chat_request_id
                )
            ),
            None,
        )

    def create_request_message(
        self,
        *,
        conversation_id,
        role,
        content,
        metadata=None,
        chat_request_id=None,
        **_kwargs,
    ):
        message = {
            "id": uuid4(),
            "conversation_id": conversation_id,
            "chat_request_id": chat_request_id,
            "role": role,
            "content": content,
            "metadata": metadata or {},
            "created_at": datetime.now(timezone.utc),
        }
        self.messages.append(message)
        return message

    def get_message(self, *, message_id, **_kwargs):
        return next(
            (message for message in self.messages if message["id"] == message_id),
            None,
        )

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


def _run_chat_service_update(
    monkeypatch,
    *,
    message=UPDATE_REQUEST,
    events=None,
    user_timezone="UTC",
):
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

    events = events or [
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
    ]
    events_by_id = {event["id"]: event for event in events}
    calendar_service = SimpleNamespace(
        list_events=AsyncMock(return_value={"events": events}),
        validate_event_write_access=AsyncMock(
            side_effect=lambda **kwargs: events_by_id[kwargs["event_id"]]
        ),
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

    def parse_spy(
        text,
        planner_time_zone=None,
        reference_time=None,
    ):
        parsed_messages.append(text)
        return original_parser(
            text,
            planner_time_zone,
            reference_time,
        )

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
    metrics_token = current_request_metrics.set(
        RequestMetrics(request_id=correlation_id)
    )
    try:
        response = service.send_message(
            user_id=USER_ID,
            conversation_id=CONVERSATION_ID,
            content=message,
            idempotency_key=uuid4(),
            user_timezone=user_timezone,
            default_calendar_connection_id=str(CONNECTION_ID),
            calendar_enabled=True,
        )
    finally:
        current_request_metrics.reset(metrics_token)
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
    caplog,
):
    caplog.set_level(logging.WARNING)
    run = _run_chat_service_update(monkeypatch)
    diagnostic = run["diagnostics"]
    user_message = run["response"]["user_message"]
    assistant_message = run["response"]["assistant_message"]
    initial_state, graph_result = run["graph_calls"][0]

    assert user_message["content"] == UPDATE_REQUEST, diagnostic
    assert initial_state["user_message"] == UPDATE_REQUEST, diagnostic
    assert graph_result["assistant_route"] == "google_calendar", diagnostic
    assert graph_result["gmail_plan"]["calendar_tool"] == (
        "update_calendar_event_proposal"
    ), diagnostic
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
    diagnostic_records = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("CALENDAR_UPDATE_DIAG_V2")
    ]
    assert diagnostic_records, diagnostic
    request_records = [
        record
        for record in diagnostic_records
        if f"request_id={diagnostic['correlation_id']}" in record
    ]
    assert {
        "chatservice_dispatch",
        "planner_normalized",
        "calendar_agent_preparse",
        "parser_validation",
        "argument_merge",
    }.issubset({
        record.split("| boundary=", 1)[1].split("|", 1)[0].strip()
        for record in request_records
        if "| boundary=" in record
    }), diagnostic_records
    assert all(
        "Personal AI Assistant - Calendar Test" not in record
        and UPDATE_REQUEST not in record
        for record in diagnostic_records
    ), diagnostic_records


def test_chat_service_persists_natural_language_title_only_update_proposal(
    monkeypatch,
):
    event = _event_at(
        "recurring-instance",
        "2026-11-10T10:00:00+05:30",
        "2026-11-10T11:00:00+05:30",
    )
    event["summary"] = "A recurring google workout"
    event["recurrence"] = ["RRULE:FREQ=WEEKLY"]
    event["recurring_event_id"] = "recurring-series"
    event["original_start_time"] = event["start"]
    run = _run_chat_service_update(
        monkeypatch,
        message=NATURAL_TITLE_UPDATE_REQUEST,
        events=[event],
        user_timezone="Asia/Kolkata",
    )
    graph_result = run["graph_calls"][0][1]

    assert graph_result["assistant_route"] == "google_calendar"
    assert "calendar_action_proposal" in graph_result
    assert len(run["saved_proposals"]) == 1
    _proposal_id, payload = run["saved_proposals"][0]
    assert payload.action_type == "calendar.update_event"
    assert payload.connection_id == CONNECTION_ID
    assert payload.parameters["calendar_id"] == "primary"
    assert payload.parameters["event_id"] == "recurring-instance"
    assert payload.parameters["expected_etag"] == '"etag-recurring-instance"'
    assert payload.parameters["current_event"]["summary"] == (
        "A recurring google workout"
    )
    assert payload.parameters["current_event"]["start"]["timeZone"] == (
        "Asia/Kolkata"
    )
    assert payload.parameters["changes"] == {"summary": "Gym"}
    assert "approve and separately execute" in graph_result["assistant_response"]
    lookup = run["calendar_service"].list_events.await_args.kwargs
    assert lookup["query"] == "A recurring google"
    assert lookup["time_min"].isoformat() == "2026-11-10T00:00:00+05:30"
    assert lookup["time_max"].isoformat() == "2026-11-11T00:00:00+05:30"


def test_chat_service_recurring_create_persists_and_serializes_approval_card(
    monkeypatch,
):
    saved_proposals = []
    proposal_created_at = datetime.now(timezone.utc)
    graph_calls = []
    original_invoke = chat_service_module.chat_graph.invoke

    def invoke_spy(state, *args, **kwargs):
        result = original_invoke(state, *args, **kwargs)
        graph_calls.append((state, result))
        return result

    monkeypatch.setattr(chat_service_module.chat_graph, "invoke", invoke_spy)

    class FakeActionApprovalService:
        def __init__(self, _db):
            pass

        def create_proposal(self, *, proposal_id, payload, **_kwargs):
            saved_proposals.append((proposal_id, payload))
            return SimpleNamespace(
                model_dump=lambda mode="python": {
                    "proposal_id": str(proposal_id),
                    "source_message_id": str(payload.source_message_id),
                    "user_id": str(USER_ID),
                    "conversation_id": str(CONVERSATION_ID),
                    "action_type": payload.action_type,
                    "parameters": payload.parameters,
                    "connection_id": str(CONNECTION_ID),
                    "account_email": "owner@example.com",
                    "created_at": proposal_created_at.isoformat(),
                    "expires_at": (
                        proposal_created_at + timedelta(minutes=15)
                    ).isoformat(),
                    "status": "pending",
                    "execution_available": False,
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
    service, _message_service = _make_service()

    response = service.send_message(
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        content=RECURRING_CREATE_REQUEST,
        idempotency_key=uuid4(),
        user_timezone="Asia/Kolkata",
        default_calendar_connection_id=str(CONNECTION_ID),
        calendar_enabled=True,
    )
    serialized = ChatResponse.model_validate(response).model_dump(mode="json")

    assert len(saved_proposals) == 1
    saved_proposal_id, payload = saved_proposals[0]
    assert payload.action_type == "calendar.create_event"
    for key, value in RECURRING_CREATE_PARAMETERS.items():
        assert payload.parameters[key] == value
    assert graph_calls[0][0]["user_message"] == RECURRING_CREATE_REQUEST
    graph_result = graph_calls[0][1]
    assert graph_result["calendar_create_event_proposal"] == (
        RECURRING_CREATE_PARAMETERS
    )
    assert graph_result["assistant_response"].startswith(
        "I prepared an approval request to create **Gym**"
    )
    approval = serialized["assistant_message"]["metadata"]["action_approval"]
    assert approval["proposal_id"] == str(saved_proposal_id)
    assert approval["parameters"]["recurrence"] == ["RRULE:FREQ=DAILY"]
    assert approval["parameters"]["start"] == (
        "2026-11-11T10:00:00+05:30"
    )


def test_completed_chat_replay_restores_saved_calendar_approval_metadata():
    service, message_service = _make_service()
    assistant_message = {
        "id": uuid4(),
        "conversation_id": CONVERSATION_ID,
        "role": "assistant",
        "content": "I prepared an approval request to create **Gym**.",
        "metadata": {"provider": "test", "model": "test-model"},
        "created_at": datetime.now(timezone.utc),
    }
    user_message = {
        "id": uuid4(),
        "conversation_id": CONVERSATION_ID,
        "role": "user",
        "content": RECURRING_CREATE_REQUEST,
        "metadata": {},
        "created_at": datetime.now(timezone.utc),
    }
    proposal_id = uuid4()
    proposal_message = {
        "id": proposal_id,
        "conversation_id": CONVERSATION_ID,
        "role": "assistant",
        "content": "Approval proposal: calendar.create_event",
        "metadata": {
            "action_approval": {
                "proposal_id": str(proposal_id),
                "source_message_id": str(assistant_message["id"]),
                "user_id": str(USER_ID),
                "conversation_id": str(CONVERSATION_ID),
                "action_type": "calendar.create_event",
                "parameters": RECURRING_CREATE_PARAMETERS,
                "connection_id": str(CONNECTION_ID),
                "account_email": "owner@example.com",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "expires_at": (
                    datetime.now(timezone.utc) + timedelta(minutes=15)
                ).isoformat(),
                "status": "pending",
            },
        },
        "created_at": datetime.now(timezone.utc),
    }
    message_service.messages.extend([
        user_message,
        assistant_message,
        proposal_message,
    ])

    response = service._get_completed_response(
        USER_ID,
        CONVERSATION_ID,
        {
            "user_message_id": str(user_message["id"]),
            "assistant_message_id": str(assistant_message["id"]),
        },
    )

    approval = response["assistant_message"]["metadata"].get("action_approval")
    assert approval is not None
    assert approval["proposal_id"] == str(proposal_id)
    assert approval["parameters"]["recurrence"] == ["RRULE:FREQ=DAILY"]


def test_chat_service_update_uses_user_timezone_when_request_omits_timezone(
    monkeypatch,
):
    message = UPDATE_REQUEST.replace("Timezone: Asia/Kolkata\n", "")
    run = _run_chat_service_update(
        monkeypatch,
        message=message,
        user_timezone="Asia/Kolkata",
    )
    diagnostic = run["diagnostics"]
    graph_result = run["graph_calls"][0][1]

    assert graph_result["assistant_route"] == "google_calendar", diagnostic
    assert run["parsed_messages"] == [message], diagnostic
    assert "calendar_action_proposal" in graph_result, diagnostic
    assert run["calendar_service"].list_events.await_args.kwargs[
        "time_zone"
    ] == "Asia/Kolkata"
    assert len(run["saved_proposals"]) == 1


def test_timed_out_completed_chat_retry_recovers_saved_calendar_proposal(
    monkeypatch,
):
    request_id = uuid4()
    user_message_id = uuid4()
    assistant_message_id = uuid4()
    proposal_id = uuid4()
    idempotency_key = uuid4()
    request_content = (
        'Delete the entire recurring Google Calendar event series named "Gym". '
        "Calendar: Primary. Start date: November 11, 2026. "
        "Time: 10:00 AM–11:00 AM. Timezone: Asia/Kolkata. "
        "Recurrence: RRULE=DAILY."
    )
    saved_proposal = {
        "proposal_id": str(proposal_id),
        "source_message_id": str(assistant_message_id),
        "action_type": "calendar.delete_event",
        "status": "pending",
        "parameters": {
            "calendar_id": "primary",
            "event_id": "series-master",
            "delete_entire_series": True,
            "expected_etag": '"etag-series"',
            "current_event": {
                "summary": "Gym",
                "recurrence": ["RRULE:FREQ=DAILY"],
                "etag": '"etag-series"',
            },
        },
    }
    user_message = {
        "id": user_message_id,
        "role": "user",
        "content": request_content,
        "metadata": {},
    }
    assistant_message = {
        "id": assistant_message_id,
        "role": "assistant",
        "content": "I prepared a recurring-series Delete proposal.",
        "metadata": {},
    }
    proposal_message = {
        "id": proposal_id,
        "role": "assistant",
        "content": "",
        "metadata": {"action_approval": saved_proposal},
    }

    class CompletedRequestRepository:
        def create_request(self, **_kwargs):
            return {
                "id": request_id,
                "status": "completed",
                "user_message_id": user_message_id,
                "assistant_message_id": assistant_message_id,
            }, False

    class PersistedMessageService:
        def verify_conversation_ownership(self, **_kwargs):
            return True

        def get_message(self, *, message_id, **_kwargs):
            return next(
                (
                    message
                    for message in (
                        user_message,
                        assistant_message,
                        proposal_message,
                    )
                    if message["id"] == message_id
                ),
                None,
            )

        def get_conversation_messages(self, **_kwargs):
            return [user_message, assistant_message, proposal_message]

    service, _ = _make_service()
    service.request_repository = CompletedRequestRepository()
    service.message_service = PersistedMessageService()
    graph_invoke = Mock(side_effect=AssertionError("request was re-executed"))
    monkeypatch.setattr(
        chat_service_module.chat_graph,
        "invoke",
        graph_invoke,
    )

    response = service.send_message(
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        content=request_content,
        idempotency_key=idempotency_key,
        user_timezone="Asia/Kolkata",
        default_calendar_connection_id=str(CONNECTION_ID),
        calendar_enabled=True,
    )

    approval = response["assistant_message"]["metadata"]["action_approval"]
    assert approval["proposal_id"] == str(proposal_id)
    assert approval["parameters"]["delete_entire_series"] is True
    assert approval["parameters"]["event_id"] == "series-master"
    graph_invoke.assert_not_called()


def test_delete_clarification_resumes_across_chat_requests(monkeypatch):
    service, _message_service = _make_service()
    events = [
        _event_at(
            "event-one",
            "2026-11-10T10:00:00+05:30",
            "2026-11-10T10:30:00+05:30",
        ),
        _event_at(
            "event-two",
            "2026-11-12T10:00:00+05:30",
            "2026-11-12T10:30:00+05:30",
        ),
    ]
    events_by_id = {event["id"]: event for event in events}
    calendar_service = SimpleNamespace(
        list_events=AsyncMock(side_effect=[
            {"events": events, "next_page_token": None},
            {"events": [events[0]], "next_page_token": None},
        ]),
        validate_event_write_access=AsyncMock(
            side_effect=lambda **kwargs: events_by_id[kwargs["event_id"]]
        ),
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

    graph_results = []
    original_invoke = chat_service_module.chat_graph.invoke

    def invoke_spy(state, *args, **kwargs):
        result = original_invoke(state, *args, **kwargs)
        graph_results.append((state, result))
        return result

    monkeypatch.setattr(chat_service_module.chat_graph, "invoke", invoke_spy)
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
                    "user_id": str(USER_ID),
                    "conversation_id": str(CONVERSATION_ID),
                }
            )

    monkeypatch.setattr(
        chat_service_module,
        "ActionApprovalService",
        FakeActionApprovalService,
    )

    responses = []
    for content in (
        "Delete my Personal AI Assistant - Calendar Test.",
        "Personal AI Assistant - Calendar Test",
    ):
        metrics_token = current_request_metrics.set(
            RequestMetrics(request_id=str(uuid4()))
        )
        try:
            responses.append(service.send_message(
                user_id=USER_ID,
                conversation_id=CONVERSATION_ID,
                content=content,
                idempotency_key=uuid4(),
                user_timezone="Asia/Kolkata",
                default_calendar_connection_id=str(CONNECTION_ID),
                calendar_enabled=True,
            ))
        finally:
            current_request_metrics.reset(metrics_token)

    first_pending = (
        responses[0]["assistant_message"]["metadata"]["pending_calendar_action"]
    )
    assert first_pending["operation"] == "delete"
    assert first_pending["connection_id"] == str(CONNECTION_ID)
    assert first_pending["calendar_id"] == "primary"
    assert first_pending["user_id"] == str(USER_ID)
    assert first_pending["conversation_id"] == str(CONVERSATION_ID)

    _first_state, first_result = graph_results[0]
    second_state, second_result = graph_results[1]
    assert first_result["assistant_route"] == "google_calendar"
    assert "multiple possible Calendar events" in (
        first_result["assistant_response"]
    )
    assert second_state["pending_calendar_action"]["operation"] == "delete"
    assert second_result["assistant_route"] == "google_calendar"
    assert second_result["gmail_plan"]["calendar_tool"] == (
        "delete_calendar_event_proposal"
    )
    assert calendar_service.list_events.await_count == 2, (
        second_result.get("assistant_response"),
        second_result.get("calendar_action_request"),
    )
    assert calendar_service.list_events.await_args.kwargs["query"] == (
        "Personal AI Assistant - Calendar Test"
    )
    assert len(saved_proposals) == 1
    _proposal_id, proposal = saved_proposals[0]
    assert proposal.action_type == "calendar.delete_event"
    assert proposal.parameters["event_id"] == "event-one"
    assert responses[1]["assistant_message"]["metadata"]["action_approval"][
        "status"
    ] == "pending"


@pytest.mark.parametrize(
    ("user_id", "conversation_id", "connection_id"),
    [
        (UUID("00000000-0000-0000-0000-000000000001"), CONVERSATION_ID, str(CONNECTION_ID)),
        (USER_ID, UUID("00000000-0000-0000-0000-000000000002"), str(CONNECTION_ID)),
        (USER_ID, CONVERSATION_ID, str(uuid4())),
    ],
)
def test_pending_calendar_action_is_scoped_to_user_conversation_and_connection(
    user_id,
    conversation_id,
    connection_id,
):
    now = datetime.now(timezone.utc)
    pending = {
        "version": 1,
        "operation": "delete",
        "original_request": "Delete my Calendar Test.",
        "user_id": str(USER_ID),
        "conversation_id": str(CONVERSATION_ID),
        "connection_id": str(CONNECTION_ID),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
    }

    assert ChatService._pending_calendar_action(
        [{
            "role": "assistant",
            "metadata": {"pending_calendar_action": pending},
        }],
        user_id=user_id,
        conversation_id=conversation_id,
        connection_id=connection_id,
        now=now,
    ) is None


def test_chat_service_cancellation_clears_pending_calendar_action(monkeypatch):
    service, _message_service = _make_service()
    graph_calls = []

    def invoke(state, *args, **kwargs):
        graph_calls.append(state)
        if state["user_message"] == "Never mind":
            return {
                "assistant_route": "google_calendar",
                "gmail_plan": {"intent": "calendar_action_cancelled"},
                "assistant_response": (
                    "The pending Google Calendar request was canceled. "
                    "No Calendar action was prepared or executed."
                ),
                "provider": "",
                "model": "",
            }
        return {
            "assistant_route": "google_calendar",
            "gmail_plan": {
                "intent": "google_calendar",
                "calendar_tool": "delete_calendar_event_proposal",
                "calendar_arguments": None,
            },
            "assistant_response": (
                "I found multiple possible Calendar events. Which one do "
                "you mean?"
            ),
            "provider": "",
            "model": "",
        }

    monkeypatch.setattr(chat_service_module.chat_graph, "invoke", invoke)
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

    responses = []
    for content in (
        "Delete my Calendar Test.",
        "Never mind",
    ):
        token = current_request_metrics.set(
            RequestMetrics(request_id=str(uuid4()))
        )
        try:
            responses.append(service.send_message(
                user_id=USER_ID,
                conversation_id=CONVERSATION_ID,
                content=content,
                idempotency_key=uuid4(),
                user_timezone="UTC",
                default_calendar_connection_id=str(CONNECTION_ID),
                calendar_enabled=True,
            ))
        finally:
            current_request_metrics.reset(token)

    assert "pending_calendar_action" in graph_calls[1]
    assert graph_calls[1]["pending_calendar_action"]["operation"] == "delete"
    assert responses[1]["assistant_message"]["metadata"].get(
        "pending_calendar_action"
    ) is None


def test_pending_calendar_action_is_available_only_to_its_owner():
    now = datetime.now(timezone.utc)
    pending = {
        "version": 1,
        "operation": "delete",
        "original_request": "Delete my Calendar Test.",
        "user_id": str(USER_ID),
        "conversation_id": str(CONVERSATION_ID),
        "connection_id": str(CONNECTION_ID),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
    }

    restored = ChatService._pending_calendar_action(
        [{
            "role": "assistant",
            "metadata": {"pending_calendar_action": pending},
        }],
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        connection_id=str(CONNECTION_ID),
        now=now,
    )

    assert restored == pending


@pytest.mark.parametrize(
    ("operation", "first_message", "clarification", "tool"),
    [
        (
            "create",
            "Create a meeting tomorrow at 10 AM.",
            "Gym",
            "create_calendar_event_proposal",
        ),
        (
            "update",
            "Update my meeting.",
            "Change title to Project Discussion.",
            "update_calendar_event_proposal",
        ),
    ],
)
def test_chat_service_restores_create_and_update_clarifications(
    monkeypatch,
    operation,
    first_message,
    clarification,
    tool,
):
    service, _message_service = _make_service()
    graph_calls = []
    second_routed = []
    initial_draft = (
        {
            "summary": "Gym",
            "event_date": "2026-11-11",
            "start_time": "10:00",
            "time_zone": "Asia/Kolkata",
            "calendar_id": "primary",
        }
        if operation == "create"
        else None
    )

    def invoke(state, *args, **kwargs):
        graph_calls.append(state)
        if len(graph_calls) == 1:
            result = {
                "assistant_route": "google_calendar",
                "gmail_plan": {
                    "intent": "google_calendar",
                    "calendar_tool": tool,
                    "calendar_arguments": {
                        "calendar_id": "primary",
                    },
                },
                "assistant_response": (
                    "What event details should I use?"
                    if operation == "create"
                    else "What would you like to change about the event?"
                ),
                "provider": "",
                "model": "",
            }
            if initial_draft is not None:
                result["calendar_create_draft"] = initial_draft
            return result
        routed = gmail_agent_nodes.gmail_intent_router_node(state)
        second_routed.append(routed)
        return {
            **routed,
            "assistant_response": (
                "Please provide the remaining Calendar details."
            ),
            "provider": "",
            "model": "",
        }

    monkeypatch.setattr(chat_service_module.chat_graph, "invoke", invoke)
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

    for content in (first_message, clarification):
        token = current_request_metrics.set(
            RequestMetrics(request_id=str(uuid4()))
        )
        try:
            service.send_message(
                user_id=USER_ID,
                conversation_id=CONVERSATION_ID,
                content=content,
                idempotency_key=uuid4(),
                user_timezone="Asia/Kolkata",
                default_calendar_connection_id=str(CONNECTION_ID),
                calendar_enabled=True,
            )
        finally:
            current_request_metrics.reset(token)

    assert graph_calls[1]["pending_calendar_action"]["operation"] == operation
    assert graph_calls[1]["pending_calendar_action"]["connection_id"] == str(
        CONNECTION_ID
    )
    assert second_routed[0]["gmail_plan"]["calendar_tool"] == tool
    assert second_routed[0]["assistant_route"] == "google_calendar"
