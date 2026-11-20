from contextlib import nullcontext
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest

from app.core.request_metrics import RequestMetrics, current_request_metrics
from app.llm.schemas import LLMResponse
from app.services import chat_service as chat_service_module
from app.services.chat_service import ChatService
from app.workflows import calendar_agent_node
from app.workflows import chat_nodes
from app.workflows import contacts_agent_node
from app.workflows import gmail_agent_nodes
from app.workflows.chat_graph import ASSISTANT_ROUTE_NODES

USER_ID = UUID("c0cd2d85-c90c-4c62-a886-c41bec1c41af")
CONVERSATION_ID = UUID("c7557e52-cd75-4ea6-9130-e927176518a8")
CALENDAR_CONNECTION_ID = UUID("5a3d3426-f1fd-4cc9-ae83-08181848cde7")
GMAIL_CONNECTION_ID = UUID("bbbbbbbb-cccc-dddd-eeee-ffffffffffff")
CONTACTS_CONNECTION_ID = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
TZ_NAME = "Asia/Kolkata"
TZ = ZoneInfo(TZ_NAME)

DELETE_REQUEST = "delete the event from 15th november gym time"

CALENDAR_DETAILS = (
    "Title: Lunch\n"
    "Date: November 15, 2026\n"
    "Start: 2:00 PM\n"
    "End: 3:00 PM\n"
    "Timezone: Asia/Kolkata\n"
    "Calendar: primary\n"
)


def _lunch_event(event_id, start="2026-11-15T14:00:00+05:30", end="2026-11-15T15:00:00+05:30"):
    return _event(event_id, "Lunch", start, end)


@pytest.fixture(autouse=True)
def _disable_llm_chatter(monkeypatch):
    monkeypatch.setattr(
        gmail_agent_nodes.llm_router,
        "generate",
        Mock(side_effect=RuntimeError("planner disabled in test")),
    )
    monkeypatch.setattr(
        chat_nodes.llm_router,
        "generate",
        Mock(
            return_value=LLMResponse(
                content="OK",
                provider="test",
                model="test",
                finish_reason="stop",
                latency_ms=0,
                output_limited=False,
            )
        ),
    )


class FakeRequestRepository:
    def __init__(self):
        self.request_ids = []

    def create_request(self, **_kwargs):
        request_id = uuid4()
        self.request_ids.append(request_id)
        return {"id": request_id, "status": "processing"}, True

    def claim_request(self, **_kwargs):
        return {"id": self.request_ids[-1]}

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
        for message in reversed(self.messages):
            if message["role"] != role:
                continue
            if (
                chat_request_id is not None
                and message.get("chat_request_id") != chat_request_id
            ):
                continue
            return message
        return None

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
        return [dict(message) for message in self.messages]

    def update_request_message_content(
        self, *, message_id, content, metadata=None, **_kwargs
    ):
        for message in self.messages:
            if message["id"] == message_id:
                message["content"] = content
                if metadata is not None:
                    message["metadata"] = metadata
                return message
        return None


class FakeUserConnectionService:
    db = object()

    def get_granted_google_scopes(self, **_kwargs):
        return set()

    def get_default_google_connection_for_scopes(self, **_kwargs):
        return None

    def get_user_calendar_access(self, **_kwargs):
        return {
            "google_connection_available": True,
            "capabilities": {
                "write_permission_granted": True,
                "calendar_write_execution_available": False,
                "calendar_update_execution_available": False,
                "calendar_create_execution_available": False,
                "calendar_delete_execution_available": True,
                "calendar_update_feature_enabled": False,
            },
            "event_read_connection_id": str(CALENDAR_CONNECTION_ID),
            "freebusy_connection_id": None,
            "write_connection_id": str(CALENDAR_CONNECTION_ID),
            "default_connection_invalid": False,
            "default_connection_selected": True,
            "selected_connection_email": "user@example.com",
            "event_selection_required": False,
            "freebusy_selection_required": False,
            "selected_event_scope_missing": False,
            "selected_freebusy_scope_missing": False,
            "calendar_write_selection_required": False,
            "calendar_selected_write_scope_missing": False,
        }

    def get_user_contacts_access(self, **_kwargs):
        return {
            "google_connection_available": True,
            "capabilities": {},
            "read_connection_id": str(CONTACTS_CONNECTION_ID),
            "write_connection_id": None,
            "selected_connection_email": "user@example.com",
            "default_connection_invalid": False,
            "default_connection_selected": True,
            "selection_required": False,
            "candidate_emails": ["user@example.com"],
            "read_scope_missing": False,
            "write_scope_missing": False,
        }

    def get_user_drive_access(self, **_kwargs):
        return {
            "google_connection_available": False,
            "capabilities": {},
            "connection_id": None,
            "selected_connection_email": None,
            "default_connection_invalid": False,
            "default_connection_selected": False,
            "selection_required": False,
            "candidate_emails": [],
            "read_scope_missing": False,
            "enabled": False,
        }


def _make_service(message_service):
    return ChatService(
        message_service=message_service,
        user_settings_service=SimpleNamespace(
            get_settings=Mock(return_value=SimpleNamespace(memory_enabled=False))
        ),
        memory_pipeline=SimpleNamespace(process=Mock(return_value=None)),
        request_repository=FakeRequestRepository(),
        user_connection_service=FakeUserConnectionService(),
    )


def _event(event_id, summary, start, end):
    return {
        "id": event_id,
        "summary": summary,
        "description": None,
        "location": None,
        "status": "confirmed",
        "html_link": f"https://calendar.google.com/event?eid={event_id}",
        "etag": f'"{event_id}-etag"',
        "start": {
            "date": None,
            "date_time": datetime.fromisoformat(start),
            "time_zone": TZ_NAME,
        },
        "end": {
            "date": None,
            "date_time": datetime.fromisoformat(end),
            "time_zone": TZ_NAME,
        },
        "attendees": [],
        "recurrence": None,
        "recurring_event_id": None,
        "original_start_time": None,
        "updated": None,
    }


def _candidate(event):
    return {
        "event_id": event["id"],
        "calendar_id": "primary",
        "summary": event["summary"],
        "start": event["start"]["date_time"].isoformat(),
        "end": event["end"]["date_time"].isoformat(),
        "time_zone": TZ_NAME,
    }


def _pending_selection(candidates, **overrides):
    pending = {
        "version": 1,
        "operation": "delete_selection",
        "user_id": str(USER_ID),
        "conversation_id": str(CONVERSATION_ID),
        "connection_id": str(CALENDAR_CONNECTION_ID),
        "calendar_id": "primary",
        "candidates": candidates,
        "created_at": "2026-11-14T14:55:00+05:30",
        "expires_at": "2026-11-14T15:05:00+05:30",
        "original_request": DELETE_REQUEST,
    }
    pending.update(overrides)
    return pending


def _setup(monkeypatch, events=None):
    monkeypatch.setattr(
        ChatService,
        "_start_heartbeat",
        lambda *_args, **_kwargs: (None, None, None),
    )
    monkeypatch.setattr(ChatService, "_stop_heartbeat", lambda *_args, **_kwargs: None)

    if events is None:
        first = _event(
            "evt-real-gym-time-for",
            "Gym time for",
            "2026-11-15T09:00:00+05:30",
            "2026-11-15T10:00:00+05:30",
        )
        second = _event(
            "evt-real-gym-other",
            "Gym time with trainer",
            "2026-11-15T18:00:00+05:30",
            "2026-11-15T19:00:00+05:30",
        )
        events = [first, second]
    by_id = {event["id"]: event for event in events}
    calls = {
        "calendar_list": [],
        "calendar_get": [],
        "calendar_validate": [],
        "calendar_delete": [],
        "gmail": [],
        "contacts": [],
        "proposals": [],
        "invoke": [],
        "stream": [],
        "calendar_service": None,
    }

    calendar_service = SimpleNamespace(
        list_events=AsyncMock(
            side_effect=lambda **kwargs: (
                calls["calendar_list"].append(kwargs),
                {
                    "events": events,
                    "next_page_token": None,
                    "calendar_id": kwargs.get("calendar_id", "primary"),
                },
            )[1]
        ),
        get_event=AsyncMock(
            side_effect=lambda **kwargs: (
                calls["calendar_get"].append(kwargs),
                by_id.get(kwargs.get("event_id")),
            )[1]
        ),
        validate_event_write_access=AsyncMock(
            side_effect=lambda **kwargs: (
                calls["calendar_validate"].append(kwargs),
                by_id.get(kwargs.get("event_id")),
            )[1]
        ),
        list_calendars=AsyncMock(
            return_value={"calendars": [], "next_page_token": None}
        ),
        delete_event=AsyncMock(
            side_effect=lambda **kwargs: (
                calls["calendar_delete"].append(kwargs),
                {"deleted": True},
            )[1]
        ),
    )
    monkeypatch.setattr(
        calendar_agent_node, "SessionLocal", lambda: nullcontext(object())
    )
    monkeypatch.setattr(
        calendar_agent_node, "CalendarService", lambda _db: calendar_service
    )
    calls["calendar_service"] = calendar_service

    async def fake_execute_gmail_read(*, plan, **_kwargs):
        calls["gmail"].append(plan)
        return {"messages": [], "period": "unknown", "search_criteria": ""}

    monkeypatch.setattr(
        gmail_agent_nodes,
        "execute_gmail_read",
        fake_execute_gmail_read,
        raising=False,
    )

    def fake_contacts_response(**kwargs):
        calls["contacts"].append(kwargs)
        return {
            "assistant_response": "I found one contact.",
            "contacts_provenance": {
                "outcome_status": "single_match",
                "verified_source": True,
            },
            "pending_contacts_clarification": None,
        }

    monkeypatch.setattr(
        contacts_agent_node, "generate_contacts_read_response", fake_contacts_response
    )

    class FakeApprovalService:
        def __init__(self, _db):
            pass

        def create_proposal(self, **kwargs):
            payload = kwargs["payload"]
            parameters = getattr(payload, "parameters", None)
            action_type = getattr(payload, "action_type", None)
            calls["proposals"].append(
                {"action_type": action_type, "parameters": parameters}
            )
            return SimpleNamespace(
                model_dump=lambda mode="json": {
                    "proposal_id": str(uuid4()),
                    "action_type": action_type,
                    "parameters": parameters,
                    "status": "pending",
                }
            )

    monkeypatch.setattr(
        chat_service_module, "ActionApprovalService", FakeApprovalService
    )

    original_invoke = chat_service_module.chat_graph.invoke

    def invoke_spy(state, *args, **kwargs):
        result = original_invoke(state, *args, **kwargs)
        calls["invoke"].append({"state_in": state, "result": result})
        return result

    monkeypatch.setattr(chat_service_module.chat_graph, "invoke", invoke_spy)

    original_stream = chat_service_module.chat_graph.stream

    def stream_spy(state, *args, **kwargs):
        record = {"state_in": state, "events": []}
        calls["stream"].append(record)
        for event in original_stream(state, *args, **kwargs):
            record["events"].append(event)
            yield event

    monkeypatch.setattr(chat_service_module.chat_graph, "stream", stream_spy)

    return calls, message_service_holder()


def message_service_holder():
    holder = SimpleNamespace(service=None)

    def _make():
        holder.service = FakeMessageService()
        return holder.service

    holder.make = _make
    return holder


def _send(service, content, *, streaming=False):
    kwargs = {
        "user_id": USER_ID,
        "conversation_id": CONVERSATION_ID,
        "content": content,
        "idempotency_key": uuid4(),
        "user_timezone": TZ_NAME,
        "gmail_connection_id": GMAIL_CONNECTION_ID,
        "default_calendar_connection_id": str(CALENDAR_CONNECTION_ID),
        "default_contacts_connection_id": str(CONTACTS_CONNECTION_ID),
        "gmail_enabled": True,
        "calendar_enabled": True,
        "contacts_enabled": True,
        "drive_enabled": False,
    }
    token = current_request_metrics.set(RequestMetrics(request_id=str(uuid4())))
    try:
        if not streaming:
            return service.send_message(**kwargs), None
        events = list(service.stream_message(**kwargs))
        response = next(
            event["response"] for event in events if event.get("type") == "done"
        )
        return response, events
    finally:
        current_request_metrics.reset(token)


def _assert_no_calendar_mutation(calls):
    assert calls["proposals"] == []
    assert calls["calendar_delete"] == []
    assert calls["calendar_validate"] == []


def _assert_no_delete_or_proposal(calls):
    assert calls["proposals"] == []
    assert calls["calendar_delete"] == []


def _tool_counts(calls):
    return {
        key: len(calls[key])
        for key in (
            "calendar_list",
            "calendar_validate",
            "calendar_delete",
            "proposals",
            "gmail",
            "contacts",
        )
    }


def _create_clarification(monkeypatch):
    calls, holder = _setup(monkeypatch)
    message_service = holder.make()
    service = _make_service(message_service)
    first, _ = _send(service, DELETE_REQUEST)
    metadata = first["assistant_message"]["metadata"]
    pending = metadata["pending_calendar_delete_selection"]
    assert [candidate["event_id"] for candidate in pending["candidates"]] == [
        "evt-real-gym-time-for",
        "evt-real-gym-other",
    ]
    return calls, message_service, service


@pytest.mark.parametrize(
    ("message", "expected_route", "expected_intent", "expect_gmail_tool"),
    (
        ("check my last mail", "gmail", "get_latest_email", True),
        ("show my unread emails", "gmail", "summarize_unread", True),
        ("search Gmail for invoices", "gmail", "clarify", False),
        ("show my contacts", "google_contacts", "google_contacts", False),
        ("what is the capital of France?", "general", "general", False),
    ),
)
def test_fresh_domain_request_is_not_hijacked_by_calendar_clarification(
    monkeypatch,
    message,
    expected_route,
    expected_intent,
    expect_gmail_tool,
):
    calls, message_service, service = _create_clarification(monkeypatch)
    list_count_before = len(calls["calendar_list"])
    gmail_count_before = len(calls["gmail"])
    contacts_count_before = len(calls["contacts"])

    response, _ = _send(service, message)

    graph_result = calls["invoke"][-1]["result"]
    route = graph_result["assistant_route"]
    assert route == expected_route
    assert ASSISTANT_ROUTE_NODES[route] == {
        "gmail": "gmail_agent",
        "google_contacts": "contacts_agent",
        "general": "memory_retrieval",
    }[expected_route]
    assert (graph_result.get("gmail_plan") or {}).get("intent") == expected_intent

    if expect_gmail_tool:
        assert len(calls["gmail"]) > gmail_count_before
    if expected_route == "google_contacts":
        assert len(calls["contacts"]) > contacts_count_before

    assert len(calls["calendar_list"]) == list_count_before
    _assert_no_calendar_mutation(calls)

    latest_metadata = response["assistant_message"]["metadata"]
    assert "pending_calendar_delete_selection" not in latest_metadata
    assert any(
        item["role"] == "assistant"
        and isinstance(
            item["metadata"].get("pending_calendar_delete_selection"), dict
        )
        for item in message_service.messages
    )


@pytest.mark.parametrize(
    ("reply", "expected_event_id"),
    (
        ("option 1", "evt-real-gym-time-for"),
        ("the second one", "evt-real-gym-other"),
    ),
)
def test_genuine_selection_reply_resumes_suspended_calendar_context(
    monkeypatch, reply, expected_event_id
):
    calls, _message_service, service = _create_clarification(monkeypatch)
    _send(service, "check my last mail")
    list_count_before = len(calls["calendar_list"])

    _send(service, reply)

    graph_result = calls["invoke"][-1]["result"]
    assert graph_result["assistant_route"] == "google_calendar"
    assert ASSISTANT_ROUTE_NODES["google_calendar"] == "calendar_agent"
    assert (graph_result.get("gmail_plan") or {}).get("calendar_tool") == (
        "delete_calendar_event_proposal"
    )
    assert len(calls["proposals"]) == 1
    assert calls["proposals"][0]["parameters"]["event_id"] == expected_event_id
    assert len(calls["calendar_list"]) == list_count_before
    assert calls["calendar_delete"] == []


def test_short_calendar_title_reply_keeps_focused_clarification(monkeypatch):
    calls, _message_service, service = _create_clarification(monkeypatch)
    list_count_before = len(calls["calendar_list"])

    response, _ = _send(service, "gym time for")

    graph_result = calls["invoke"][-1]["result"]
    assert graph_result["assistant_route"] == "google_calendar"
    assert (graph_result.get("gmail_plan") or {}).get("calendar_tool") == "clarify"
    assert "more than one matching Calendar event" in response["assistant_message"]["content"]
    retained = response["assistant_message"]["metadata"][
        "pending_calendar_delete_selection"
    ]
    assert [item["event_id"] for item in retained["candidates"]] == [
        "evt-real-gym-time-for",
        "evt-real-gym-other",
    ]
    assert len(calls["calendar_list"]) == list_count_before
    _assert_no_calendar_mutation(calls)


def test_explicit_new_calendar_request_is_not_swallowed_by_pending_state(monkeypatch):
    calls, _message_service, service = _create_clarification(monkeypatch)

    response, _ = _send(service, "check my calendar tomorrow")

    graph_result = calls["invoke"][-1]["result"]
    assert graph_result["assistant_route"] == "google_calendar"
    assert ASSISTANT_ROUTE_NODES["google_calendar"] == "calendar_agent"
    assert graph_result.get("calendar_action_request") is None
    _assert_no_calendar_mutation(calls)
    assert response["assistant_message"]["content"].strip()


def test_pending_selection_is_scoped_to_user_conversation_and_connection():
    pending = _pending_selection([_candidate(_event(
        "evt-real-gym-time-for",
        "Gym time for",
        "2026-11-15T09:00:00+05:30",
        "2026-11-15T10:00:00+05:30",
    ))])
    history = [{"role": "assistant", "metadata": {
        "pending_calendar_delete_selection": pending,
    }}]
    now = datetime(2026, 11, 14, 10, 0, tzinfo=TZ)

    assert ChatService._pending_calendar_delete_selection(
        history,
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        connection_id=str(CALENDAR_CONNECTION_ID),
        now=now,
    ) == pending
    # No first-class tenant exists in this path; authenticated user_id is the
    # effective tenant boundary.
    assert ChatService._pending_calendar_delete_selection(
        history,
        user_id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        conversation_id=CONVERSATION_ID,
        connection_id=str(CALENDAR_CONNECTION_ID),
        now=now,
    ) is None
    assert ChatService._pending_calendar_delete_selection(
        history,
        user_id=USER_ID,
        conversation_id=uuid4(),
        connection_id=str(CALENDAR_CONNECTION_ID),
        now=now,
    ) is None
    assert ChatService._pending_calendar_delete_selection(
        history,
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        connection_id=str(uuid4()),
        now=now,
    ) is None


@pytest.mark.parametrize(
    "overrides",
    (
        {"expires_at": "2026-11-14T09:00:00+05:30"},
        {"version": 99},
        {"operation": "delete"},
        {"candidates": []},
    ),
)
def test_expired_or_invalid_pending_selection_is_not_restored(overrides):
    pending = _pending_selection([_candidate(_event(
        "evt-real-gym-time-for",
        "Gym time for",
        "2026-11-15T09:00:00+05:30",
        "2026-11-15T10:00:00+05:30",
    ))])
    pending.update(overrides)
    assert ChatService._pending_calendar_delete_selection(
        [{"role": "assistant", "metadata": {
            "pending_calendar_delete_selection": pending,
        }}],
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        connection_id=str(CALENDAR_CONNECTION_ID),
        now=datetime(2026, 11, 14, 10, 0, tzinfo=TZ),
    ) is None


def test_stale_candidate_cannot_authorize_calendar_mutation(monkeypatch):
    calls, _holder = _setup(monkeypatch)
    stale_candidate = {
        "event_id": "evt-not-in-google-calendar",
        "calendar_id": "primary",
        "summary": "Gym time for",
        "start": "2026-11-15T09:00:00+05:30",
        "end": "2026-11-15T10:00:00+05:30",
        "time_zone": TZ_NAME,
    }
    pending = _pending_selection([stale_candidate])
    state = {
        "user_message": "option 1",
        "gmail_plan": {},
        "assistant_route": "",
        "user_id": str(USER_ID),
        "conversation_id": str(CONVERSATION_ID),
        "user_timezone": TZ_NAME,
        "request_time": "2026-11-14T15:00:00+05:30",
        "assistant_response": "",
        "provider": "",
        "model": "",
        "messages": [],
        "pending_calendar_delete_selection": pending,
        "assistant_capabilities": {
            "google_calendar": {"write_permission_granted": True},
        },
    }
    route = gmail_agent_nodes.gmail_intent_router_node(dict(state))
    result = calendar_agent_node.calendar_agent_node(
        {**state, **route},
        SimpleNamespace(
            context={
                "user_id": str(USER_ID),
                "calendar_write_connection_id": str(CALENDAR_CONNECTION_ID),
                "google_connection_available": True,
                "calendar_enabled": True,
            }
        ),
    )

    assert result.get("calendar_action_proposal") is None
    _assert_no_delete_or_proposal(calls)


def test_streaming_gmail_request_also_suspends_calendar_clarification(monkeypatch):
    calls, _message_service, service = _create_clarification(monkeypatch)
    list_count_before = len(calls["calendar_list"])
    gmail_count_before = len(calls["gmail"])
    invoke_count_before = len(calls["invoke"])

    response, _events = _send(service, "check my last mail", streaming=True)

    assert len(calls["invoke"]) == invoke_count_before
    stream_call = next(
        record
        for record in calls["stream"]
        if record["state_in"].get("user_message") == "check my last mail"
    )
    routes = [
        payload.get("assistant_route")
        for mode, payload in stream_call["events"]
        if mode == "values" and isinstance(payload, dict)
    ]
    assert "gmail" in routes
    assert ASSISTANT_ROUTE_NODES["gmail"] == "gmail_agent"
    assert len(calls["gmail"]) > gmail_count_before
    assert len(calls["calendar_list"]) == list_count_before
    _assert_no_calendar_mutation(calls)

    latest_metadata = response["assistant_message"]["metadata"]
    assert "pending_calendar_delete_selection" not in latest_metadata


def test_exact_phase27_three_message_sequence_through_chat_service(monkeypatch):
    calls, holder = _setup(monkeypatch)
    message_service = holder.make()
    service = _make_service(message_service)

    first, _ = _send(service, DELETE_REQUEST)
    first_result = calls["invoke"][-1]["result"]
    assert first_result["assistant_route"] == "google_calendar"
    assert ASSISTANT_ROUTE_NODES["google_calendar"] == "calendar_agent"
    assert "multiple possible Calendar events" in first["assistant_message"]["content"]
    first_pending = first["assistant_message"]["metadata"][
        "pending_calendar_delete_selection"
    ]
    assert [item["event_id"] for item in first_pending["candidates"]] == [
        "evt-real-gym-time-for",
        "evt-real-gym-other",
    ]

    before_second = _tool_counts(calls)
    second, _ = _send(service, "gym time for")
    second_result = calls["invoke"][-1]["result"]
    assert second_result["assistant_route"] == "google_calendar"
    assert (second_result.get("gmail_plan") or {}).get("calendar_tool") == "clarify"
    assert (
        "more than one matching Calendar event"
        in second["assistant_message"]["content"]
    )
    second_pending = second["assistant_message"]["metadata"][
        "pending_calendar_delete_selection"
    ]
    assert [item["event_id"] for item in second_pending["candidates"]] == [
        "evt-real-gym-time-for",
        "evt-real-gym-other",
    ]
    for key, count in before_second.items():
        assert len(calls[key]) == count

    before_third = _tool_counts(calls)
    third, _ = _send(service, "check my last mail")
    third_result = calls["invoke"][-1]["result"]
    assert third_result["assistant_route"] == "gmail"
    assert ASSISTANT_ROUTE_NODES["gmail"] == "gmail_agent"
    assert (third_result.get("gmail_plan") or {}).get("intent") == (
        "get_latest_email"
    )
    assert calls["gmail"][-1]["intent"] == "get_latest_email"
    assert len(calls["gmail"]) == before_third["gmail"] + 1

    content = third["assistant_message"]["content"]
    assert "Which Calendar event" not in content
    assert "more than one matching Calendar event" not in content
    for key in (
        "calendar_list",
        "calendar_validate",
        "calendar_delete",
        "proposals",
        "contacts",
    ):
        assert len(calls[key]) == before_third[key]

    latest_metadata = third["assistant_message"]["metadata"]
    assert "pending_calendar_delete_selection" not in latest_metadata

    history = message_service.get_conversation_messages()
    now = datetime.now(timezone.utc)
    assert ChatService._pending_calendar_delete_selection(
        history,
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        connection_id=str(CALENDAR_CONNECTION_ID),
        now=now,
    ) is not None
    assert ChatService._pending_calendar_delete_selection(
        history,
        user_id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        conversation_id=CONVERSATION_ID,
        connection_id=str(CALENDAR_CONNECTION_ID),
        now=now,
    ) is None
    assert ChatService._pending_calendar_delete_selection(
        history,
        user_id=USER_ID,
        conversation_id=uuid4(),
        connection_id=str(CALENDAR_CONNECTION_ID),
        now=now,
    ) is None
    assert ChatService._pending_calendar_delete_selection(
        history,
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        connection_id=str(uuid4()),
        now=now,
    ) is None


def test_gmail_conversation_then_new_calendar_deletion_request(monkeypatch):
    calls, holder = _setup(monkeypatch)
    service = _make_service(holder.make())

    gmail_response, _ = _send(service, "check my last mail")
    gmail_result = calls["invoke"][-1]["result"]
    assert gmail_result["assistant_route"] == "gmail"
    assert (gmail_result.get("gmail_plan") or {}).get("intent") == (
        "get_latest_email"
    )
    assert len(calls["gmail"]) == 1
    assert "Which Calendar event" not in gmail_response["assistant_message"]["content"]

    before_calendar = len(calls["gmail"])
    calendar_response, _ = _send(service, DELETE_REQUEST)
    calendar_result = calls["invoke"][-1]["result"]
    assert calendar_result["assistant_route"] == "google_calendar"
    assert ASSISTANT_ROUTE_NODES["google_calendar"] == "calendar_agent"
    assert "multiple possible Calendar events" in calendar_response[
        "assistant_message"
    ]["content"]
    pending = calendar_response["assistant_message"]["metadata"][
        "pending_calendar_delete_selection"
    ]
    assert [item["event_id"] for item in pending["candidates"]] == [
        "evt-real-gym-time-for",
        "evt-real-gym-other",
    ]
    assert len(calls["gmail"]) == before_calendar
    assert len(calls["calendar_list"]) == 1
    assert calls["proposals"] == []
    assert calls["calendar_delete"] == []


def test_phase28_calendar_event_details_recognized_as_calendar_not_gmail(monkeypatch):
    """Exact Phase 28 screenshot sequence: delete -> labeled details -> Gmail must not win."""
    calls, holder = _setup(monkeypatch)
    message_service = holder.make()
    service = _make_service(message_service)

    first, _ = _send(service, "delete lunch event from november 15th")
    first_result = calls["invoke"][-1]["result"]
    assert first_result["assistant_route"] == "google_calendar"
    assert ASSISTANT_ROUTE_NODES["google_calendar"] == "calendar_agent"
    assert "existing Calendar event" in first["assistant_message"]["content"]
    assert "pending_calendar_delete_selection" not in first["assistant_message"]["metadata"]

    before = _tool_counts(calls)
    second, _ = _send(service, CALENDAR_DETAILS)
    second_result = calls["invoke"][-1]["result"]
    # Calendar event details must be recognized as Calendar, never Gmail.
    assert second_result["assistant_route"] == "google_calendar"
    assert ASSISTANT_ROUTE_NODES["google_calendar"] == "calendar_agent"
    assert len(calls["gmail"]) == before["gmail"]
    assert len(calls["calendar_list"]) == before["calendar_list"]
    assert len(calls["proposals"]) == before["proposals"]
    assert len(calls["calendar_delete"]) == before["calendar_delete"]
    content = second["assistant_message"]["content"].lower()
    assert "gmail" not in content
    assert "sender" not in content
    assert "inbox" not in content

    # Gmail still works afterward (no lingering Calendar hijack).
    third, _ = _send(service, "check my last mail")
    third_result = calls["invoke"][-1]["result"]
    assert third_result["assistant_route"] == "gmail"
    assert ASSISTANT_ROUTE_NODES["gmail"] == "gmail_agent"
    assert len(calls["gmail"]) == before["gmail"] + 1


def test_phase28_calendar_event_details_matches_pending_candidate_prepares_proposal(
    monkeypatch,
):
    lunch_early = _lunch_event("evt-lunch-early", "2026-11-15T14:00:00+05:30",
                               "2026-11-15T15:00:00+05:30")
    lunch_late = _lunch_event("evt-lunch-late", "2026-11-15T16:00:00+05:30",
                              "2026-11-15T17:00:00+05:30")
    calls, holder = _setup(monkeypatch, events=[lunch_early, lunch_late])
    service = _make_service(holder.make())

    first, _ = _send(service, "delete lunch event from november 15th")
    first_result = calls["invoke"][-1]["result"]
    assert first_result["assistant_route"] == "google_calendar"
    first_pending = first["assistant_message"]["metadata"].get(
        "pending_calendar_delete_selection"
    )
    assert first_pending is not None
    assert [c["event_id"] for c in first_pending["candidates"]] == [
        "evt-lunch-early",
        "evt-lunch-late",
    ]

    before = len(calls["proposals"])
    second, _ = _send(service, CALENDAR_DETAILS)
    second_result = calls["invoke"][-1]["result"]
    assert second_result["assistant_route"] == "google_calendar"
    assert ASSISTANT_ROUTE_NODES["google_calendar"] == "calendar_agent"
    assert (second_result.get("gmail_plan") or {}).get("calendar_tool") == (
        "delete_calendar_event_proposal"
    )
    assert len(calls["proposals"]) == before + 1
    assert calls["proposals"][-1]["parameters"]["event_id"] == "evt-lunch-early"
    assert calls["proposals"][-1]["parameters"]["calendar_id"] == "primary"
    assert calls["calendar_delete"] == []


def test_phase28_calendar_event_details_multiple_candidates_focused_clarification(
    monkeypatch,
):
    lunch_1 = _lunch_event("evt-lunch-1", "2026-11-15T14:00:00+05:30",
                           "2026-11-15T15:00:00+05:30")
    lunch_2 = _lunch_event("evt-lunch-2", "2026-11-15T14:00:00+05:30",
                           "2026-11-15T15:00:00+05:30")
    calls, holder = _setup(monkeypatch, events=[lunch_1, lunch_2])
    service = _make_service(holder.make())

    first, _ = _send(service, "delete lunch event from november 15th")
    first_pending = first["assistant_message"]["metadata"].get(
        "pending_calendar_delete_selection"
    )
    assert first_pending is not None

    second, _ = _send(service, CALENDAR_DETAILS)
    second_result = calls["invoke"][-1]["result"]
    assert second_result["assistant_route"] == "google_calendar"
    assert (second_result.get("gmail_plan") or {}).get("calendar_tool") == "clarify"
    assert "more than one matching Calendar event" in second[
        "assistant_message"
    ]["content"]
    assert calls["proposals"] == []
    assert calls["calendar_delete"] == []


def test_phase28_gmail_request_during_calendar_clarification_routes_to_gmail(
    monkeypatch,
):
    calls, holder = _setup(monkeypatch)
    service = _make_service(holder.make())

    first, _ = _send(service, DELETE_REQUEST)
    first_pending = first["assistant_message"]["metadata"].get(
        "pending_calendar_delete_selection"
    )
    assert first_pending is not None
    assert [c["event_id"] for c in first_pending["candidates"]] == [
        "evt-real-gym-time-for",
        "evt-real-gym-other",
    ]

    before = _tool_counts(calls)
    second, _ = _send(service, "check my last mail")
    second_result = calls["invoke"][-1]["result"]
    assert second_result["assistant_route"] == "gmail"
    assert ASSISTANT_ROUTE_NODES["gmail"] == "gmail_agent"
    assert (second_result.get("gmail_plan") or {}).get("intent") == "get_latest_email"
    assert len(calls["gmail"]) == before["gmail"] + 1
    assert len(calls["calendar_list"]) == before["calendar_list"]
    assert len(calls["proposals"]) == before["proposals"]
    assert len(calls["calendar_delete"]) == before["calendar_delete"]


def test_phase28_return_to_calendar_after_gmail_resumes_pending(monkeypatch):
    calls, holder = _setup(monkeypatch)
    service = _make_service(holder.make())

    first, _ = _send(service, DELETE_REQUEST)
    first_pending = first["assistant_message"]["metadata"].get(
        "pending_calendar_delete_selection"
    )
    assert first_pending is not None
    assert [c["event_id"] for c in first_pending["candidates"]] == [
        "evt-real-gym-time-for",
        "evt-real-gym-other",
    ]

    # Gmail switch suspends (does not clear) the pending Calendar context.
    _send(service, "check my last mail")

    third, _ = _send(service, "the second one")
    third_result = calls["invoke"][-1]["result"]
    assert third_result["assistant_route"] == "google_calendar"
    assert ASSISTANT_ROUTE_NODES["google_calendar"] == "calendar_agent"
    assert (third_result.get("gmail_plan") or {}).get("calendar_tool") == (
        "delete_calendar_event_proposal"
    )
    assert len(calls["proposals"]) == 1
    assert calls["proposals"][-1]["parameters"]["event_id"] == "evt-real-gym-other"
    assert calls["calendar_delete"] == []


def test_phase28_general_question_during_calendar_clarification(monkeypatch):
    calls, holder = _setup(monkeypatch)
    message_service = holder.make()
    service = _make_service(message_service)

    first, _ = _send(service, DELETE_REQUEST)
    first_pending = first["assistant_message"]["metadata"].get(
        "pending_calendar_delete_selection"
    )
    assert first_pending is not None

    before = _tool_counts(calls)
    second, _ = _send(service, "What is LangGraph?")
    second_result = calls["invoke"][-1]["result"]
    assert second_result["assistant_route"] == "general"
    assert ASSISTANT_ROUTE_NODES["general"] == "memory_retrieval"
    assert len(calls["gmail"]) == before["gmail"]
    assert len(calls["calendar_list"]) == before["calendar_list"]
    assert len(calls["proposals"]) == before["proposals"]

    # Suspended Calendar context remains restorable for same user/conversation/connection.
    now = datetime.now(timezone.utc)
    restored = ChatService._pending_calendar_delete_selection(
        message_service.get_conversation_messages(),
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        connection_id=str(CALENDAR_CONNECTION_ID),
        now=now,
    )
    assert restored is not None
    assert ChatService._pending_calendar_delete_selection(
        message_service.get_conversation_messages(),
        user_id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        conversation_id=CONVERSATION_ID,
        connection_id=str(CALENDAR_CONNECTION_ID),
        now=now,
    ) is None


def test_phase28_expired_pending_selection_not_restored():
    pending = _pending_selection([_candidate(_lunch_event("evt-lunch-noon"))])
    pending["expires_at"] = "2026-11-14T09:00:00+05:30"
    history = [{"role": "assistant", "metadata": {
        "pending_calendar_delete_selection": pending,
    }}]
    now = datetime(2026, 11, 14, 10, 0, tzinfo=TZ)

    assert ChatService._pending_calendar_delete_selection(
        history,
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        connection_id=str(CALENDAR_CONNECTION_ID),
        now=now,
    ) is None


def test_phase28_streaming_calendar_event_details_not_routed_to_gmail(monkeypatch):
    calls, holder = _setup(monkeypatch)
    service = _make_service(holder.make())

    _send(service, "delete lunch event from november 15th")

    invoke_count_before = len(calls["invoke"])
    stream_count_before = len(calls["stream"])
    gmail_count_before = len(calls["gmail"])
    calendar_list_count_before = len(calls["calendar_list"])

    response, _events = _send(service, CALENDAR_DETAILS, streaming=True)

    assert len(calls["stream"]) == stream_count_before + 1
    assert len(calls["invoke"]) == invoke_count_before
    stream_call = next(
        record
        for record in calls["stream"]
        if isinstance(record["state_in"].get("user_message"), str)
        and "Title: Lunch" in record["state_in"]["user_message"]
    )
    routes = [
        payload.get("assistant_route")
        for mode, payload in stream_call["events"]
        if mode == "values" and isinstance(payload, dict)
    ]
    assert "google_calendar" in routes
    assert ASSISTANT_ROUTE_NODES["google_calendar"] == "calendar_agent"
    assert len(calls["gmail"]) == gmail_count_before
    assert len(calls["calendar_list"]) == calendar_list_count_before
    assert calls["proposals"] == []
    assert calls["calendar_delete"] == []

    latest_metadata = response["assistant_message"]["metadata"]
    assert "pending_calendar_delete_selection" not in latest_metadata
