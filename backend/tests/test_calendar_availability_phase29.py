from contextlib import nullcontext
from datetime import date, datetime, time as datetime_time, timedelta, timezone
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
from app.workflows import calendar_tools
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

AVAILABILITY_REQUEST = "check whether I am free tomorrow in googlecalendar or not?"
FULL_DAY_REQUEST = "check full day"
EXPLICIT_TZ_REQUEST = "Check whether I am free tomorrow in Asia/Kolkata."
GMAIL_REQUEST = "check my last mail"
GENERAL_REQUEST = "What is LangGraph?"


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
                "freebusy": True,
                "api_available": True,
                "api_complete": True,
            },
            "event_read_connection_id": str(CALENDAR_CONNECTION_ID),
            "freebusy_connection_id": str(CALENDAR_CONNECTION_ID),
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


def _send(service, content, *, streaming=False, user_timezone=TZ_NAME):
    kwargs = {
        "user_id": USER_ID,
        "conversation_id": CONVERSATION_ID,
        "content": content,
        "idempotency_key": uuid4(),
        "user_timezone": user_timezone,
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


def _setup(monkeypatch, events=None, calendar_timezone=TZ_NAME):
    monkeypatch.setattr(
        ChatService,
        "_start_heartbeat",
        lambda *_args, **_kwargs: (None, None, None),
    )
    monkeypatch.setattr(ChatService, "_stop_heartbeat", lambda *_args, **_kwargs: None)

    if events is None:
        events = []

    by_id = {event["id"]: event for event in events}
    calls = {
        "calendar_list": [],
        "calendar_list_tool": [],
        "calendar_get": [],
        "calendar_validate": [],
        "calendar_delete": [],
        "gmail": [],
        "contacts": [],
        "proposals": [],
        "invoke": [],
        "stream": [],
        "calendar_service": None,
        "freebusy": [],
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

    async def fake_query_freebusy(**kwargs):
        calls["freebusy"].append(kwargs)
        return {
            "time_min": kwargs["time_min"],
            "time_max": kwargs["time_max"],
            "time_zone": kwargs["time_zone"],
            "calendars": {
                "primary": {"busy": [], "errors": []},
            },
        }

    async def fake_list_calendars(**kwargs):
        calls["calendar_list_tool"].append(kwargs)
        return {
            "calendars": [
                {
                    "id": "primary",
                    "summary": "Primary",
                    "primary": True,
                    "access_role": "owner",
                    "time_zone": calendar_timezone,
                },
            ],
            "next_page_token": None,
        }

    tool_service = SimpleNamespace(
        query_freebusy=AsyncMock(side_effect=fake_query_freebusy),
        list_calendars=AsyncMock(side_effect=fake_list_calendars),
    )
    monkeypatch.setattr(
        calendar_tools, "CalendarService", lambda _db: tool_service
    )

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

    holder = SimpleNamespace(service=None)

    def _make():
        holder.service = FakeMessageService()
        return holder.service

    holder.make = _make
    return calls, holder


def _tool_counts(calls):
    return {
        key: len(calls[key])
        for key in (
            "calendar_list",
            "calendar_list_tool",
            "calendar_validate",
            "calendar_delete",
            "proposals",
            "gmail",
            "contacts",
        )
    }


def _assert_no_calendar_activity(calls):
    assert calls["proposals"] == []
    assert calls["calendar_delete"] == []
    assert calls["calendar_validate"] == []


def _request_date(result, timezone_name):
    request_time = datetime.fromisoformat(
        str(result["request_time"]).replace("Z", "+00:00")
    )
    return request_time.astimezone(ZoneInfo(timezone_name)).date()


def _assert_freebusy_calls(calls, *, timezone_name, target_date, expected_count=1):
    assert len(calls["freebusy"]) == expected_count
    call = calls["freebusy"][-1]
    zone = ZoneInfo(timezone_name)
    assert call["user_id"] == USER_ID
    assert call["connection_id"] == CALENDAR_CONNECTION_ID
    assert call["calendar_ids"] == ["primary"]
    assert call["time_zone"] == timezone_name
    assert call["time_min"] == datetime.combine(
        target_date, datetime_time.min, tzinfo=zone
    )
    assert call["time_max"] == datetime.combine(
        target_date + timedelta(days=1), datetime_time.min, tzinfo=zone
    )


# ---------------------------------------------------------------------------
# Router-level tests (no ChatService, direct node invocation)
# ---------------------------------------------------------------------------

def _router_state(message, **overrides):
    state = {
        "user_message": message,
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
        "assistant_capabilities": {
            "google_calendar": {
                "connected": True,
                "api_available": True,
                "events_read": True,
                "write_permission_granted": True,
            }
        },
    }
    state.update(overrides)
    return state


def test_router_availability_request_routes_to_calendar():
    """Turn 1: availability request must route to google_calendar/check_calendar_availability."""
    result = gmail_agent_nodes.gmail_intent_router_node(
        _router_state(AVAILABILITY_REQUEST)
    )
    assert result["assistant_route"] == "google_calendar"
    assert ASSISTANT_ROUTE_NODES["google_calendar"] == "calendar_agent"
    plan = result.get("gmail_plan") or {}
    assert plan.get("calendar_tool") == "check_calendar_availability"
    args = plan.get("calendar_arguments") or {}
    assert args.get("time_zone") == "Asia/Kolkata"
    assert args.get("calendar_ids") == ["primary"]
    # Tomorrow's local-day boundaries in Asia/Kolkata
    assert args.get("time_min") == "2026-11-15T00:00:00+05:30"
    assert args.get("time_max") == "2026-11-16T00:00:00+05:30"


def test_router_explicit_timezone_uses_specified_zone():
    """Scenario D: explicit Asia/Kolkata in message is honored."""
    result = gmail_agent_nodes.gmail_intent_router_node(
        _router_state(EXPLICIT_TZ_REQUEST)
    )
    assert result["assistant_route"] == "google_calendar"
    plan = result.get("gmail_plan") or {}
    assert plan.get("calendar_tool") == "check_calendar_availability"
    args = plan.get("calendar_arguments") or {}
    assert args.get("time_zone") == "Asia/Kolkata"


def test_router_invalid_message_timezone_clarifies():
    """An invalid explicit timezone must clarify, never silently use UTC."""
    result = gmail_agent_nodes.gmail_intent_router_node(
        _router_state("Check whether I am free tomorrow in Mars/Olympus?")
    )
    assert result["assistant_route"] == "google_calendar"
    plan = result.get("gmail_plan") or {}
    assert plan.get("calendar_tool") == "clarify"


def test_router_invalid_user_timezone_clarifies():
    """An invalid saved timezone must clarify for availability requests."""
    result = gmail_agent_nodes.gmail_intent_router_node(
        _router_state(AVAILABILITY_REQUEST, user_timezone="Mars/Olympus")
    )
    assert result["assistant_route"] == "google_calendar"
    plan = result.get("gmail_plan") or {}
    assert plan.get("calendar_tool") == "clarify"


def test_router_googlecalendar_one_word_recognized():
    """'googlecalendar' as one word is still recognized as Calendar."""
    result = gmail_agent_nodes.gmail_intent_router_node(
        _router_state("check whether I am free tomorrow in googlecalendar or not?")
    )
    assert result["assistant_route"] == "google_calendar"
    plan = result.get("gmail_plan") or {}
    assert plan.get("calendar_tool") == "check_calendar_availability"


def test_router_pending_availability_full_day_continuation():
    """Scenario B: 'check full day' resumes pending whole-day availability."""
    pending = {
        "version": 1,
        "operation": "availability",
        "user_id": str(USER_ID),
        "conversation_id": str(CONVERSATION_ID),
        "connection_id": str(CALENDAR_CONNECTION_ID),
        "expires_at": "2026-11-14T15:10:00+05:30",
        "original_request": AVAILABILITY_REQUEST,
        "whole_day": True,
        "time_zone": "Asia/Kolkata",
        "target_date": "2026-11-15",
    }
    result = gmail_agent_nodes.gmail_intent_router_node(
        _router_state(FULL_DAY_REQUEST, pending_calendar_availability=pending)
    )
    assert result["assistant_route"] == "google_calendar"
    assert ASSISTANT_ROUTE_NODES["google_calendar"] == "calendar_agent"
    plan = result.get("gmail_plan") or {}
    assert plan.get("calendar_tool") == "check_calendar_availability"
    args = plan.get("calendar_arguments") or {}
    assert args.get("time_zone") == "Asia/Kolkata"
    assert args.get("time_min") == "2026-11-15T00:00:00+05:30"
    assert args.get("time_max") == "2026-11-16T00:00:00+05:30"


def test_router_pending_availability_timezone_alias():
    """Scenario C: 'asia india' normalizes to Asia/Kolkata and resumes availability."""
    pending = {
        "version": 1,
        "operation": "availability",
        "user_id": str(USER_ID),
        "conversation_id": str(CONVERSATION_ID),
        "connection_id": str(CALENDAR_CONNECTION_ID),
        "expires_at": "2026-11-14T15:10:00+05:30",
        "original_request": AVAILABILITY_REQUEST,
        "whole_day": True,
        "time_zone": "UTC",
        "target_date": "2026-11-15",
    }
    result = gmail_agent_nodes.gmail_intent_router_node(
        _router_state("asia india", pending_calendar_availability=pending)
    )
    assert result["assistant_route"] == "google_calendar"
    plan = result.get("gmail_plan") or {}
    assert plan.get("calendar_tool") == "check_calendar_availability"
    args = plan.get("calendar_arguments") or {}
    assert args.get("time_zone") == "Asia/Kolkata"


def test_router_pending_availability_general_question_not_hijacked():
    """Scenario F: general question during availability → general, not Calendar."""
    pending = {
        "version": 1,
        "operation": "availability",
        "user_id": str(USER_ID),
        "conversation_id": str(CONVERSATION_ID),
        "connection_id": str(CALENDAR_CONNECTION_ID),
        "expires_at": "2026-11-14T15:10:00+05:30",
        "original_request": AVAILABILITY_REQUEST,
        "whole_day": True,
        "time_zone": "Asia/Kolkata",
        "target_date": "2026-11-15",
    }
    result = gmail_agent_nodes.gmail_intent_router_node(
        _router_state(GENERAL_REQUEST, pending_calendar_availability=pending)
    )
    assert result["assistant_route"] == "general"
    assert ASSISTANT_ROUTE_NODES["general"] == "memory_retrieval"


def test_router_pending_availability_gmail_not_hijacked():
    """Scenario E part 1: Gmail request during availability → Gmail, not Calendar."""
    pending = {
        "version": 1,
        "operation": "availability",
        "user_id": str(USER_ID),
        "conversation_id": str(CONVERSATION_ID),
        "connection_id": str(CALENDAR_CONNECTION_ID),
        "expires_at": "2026-11-14T15:10:00+05:30",
        "original_request": AVAILABILITY_REQUEST,
        "whole_day": True,
        "time_zone": "Asia/Kolkata",
        "target_date": "2026-11-15",
    }
    result = gmail_agent_nodes.gmail_intent_router_node(
        _router_state(GMAIL_REQUEST, pending_calendar_availability=pending)
    )
    assert result["assistant_route"] == "gmail"
    assert ASSISTANT_ROUTE_NODES["gmail"] == "gmail_agent"


def test_normalize_timezone_aliases():
    """Timezone alias resolution: 'asia india' -> 'Asia/Kolkata'."""
    assert gmail_agent_nodes._normalize_timezone("asia india") == "Asia/Kolkata"
    assert gmail_agent_nodes._normalize_timezone("Asia/Kolkata") == "Asia/Kolkata"
    assert gmail_agent_nodes._normalize_timezone("UTC") == "UTC"
    assert gmail_agent_nodes._normalize_timezone("invalid") == "UTC"
    assert gmail_agent_nodes._normalize_timezone(None) == "UTC"
    assert gmail_agent_nodes._normalize_timezone("") == "UTC"


# ---------------------------------------------------------------------------
# ChatService-level tests (full multi-turn through LangGraph)
# ---------------------------------------------------------------------------

def test_chat_service_availability_default_timezone_executes_freebusy(
    monkeypatch,
):
    """Scenario A: availability request executes free/busy with connected tz."""
    calls, holder = _setup(monkeypatch)
    message_service = holder.make()
    service = _make_service(message_service)

    response, _ = _send(service, AVAILABILITY_REQUEST)
    result = calls["invoke"][-1]["result"]
    assert result["assistant_route"] == "google_calendar"
    assert ASSISTANT_ROUTE_NODES["google_calendar"] == "calendar_agent"
    plan = result.get("gmail_plan") or {}
    assert plan.get("calendar_tool") == "check_calendar_availability"
    args = plan.get("calendar_arguments") or {}
    assert args.get("time_zone") == "Asia/Kolkata"
    target_date = _request_date(result, TZ_NAME) + timedelta(days=1)
    assert args.get("time_min") == f"{target_date.isoformat()}T00:00:00+05:30"
    assert args.get("time_max") == (
        f"{(target_date + timedelta(days=1)).isoformat()}T00:00:00+05:30"
    )
    _assert_freebusy_calls(
        calls,
        timezone_name=TZ_NAME,
        target_date=target_date,
    )
    assert len(calls["calendar_list_tool"]) >= 1
    _assert_no_calendar_activity(calls)


def test_chat_service_connected_calendar_timezone_is_authoritative(monkeypatch):
    """Connected Calendar timezone wins over the app user timezone."""
    calls, holder = _setup(monkeypatch)
    service = _make_service(holder.make())

    response, _ = _send(service, AVAILABILITY_REQUEST, user_timezone="UTC")
    result = calls["invoke"][-1]["result"]
    plan = result.get("gmail_plan") or {}
    assert plan.get("calendar_tool") == "check_calendar_availability"
    args = plan.get("calendar_arguments") or {}
    assert args.get("time_zone") == "UTC"
    executed = result.get("calendar_executed_arguments") or {}
    assert executed.get("time_zone") == TZ_NAME
    target_date = _request_date(result, "UTC") + timedelta(days=1)
    assert executed.get("time_min") == f"{target_date.isoformat()}T00:00:00+05:30"
    assert executed.get("time_max") == (
        f"{(target_date + timedelta(days=1)).isoformat()}T00:00:00+05:30"
    )
    _assert_freebusy_calls(
        calls,
        timezone_name=TZ_NAME,
        target_date=target_date,
    )
    pending = response["assistant_message"]["metadata"]["pending_calendar_availability"]
    assert pending["time_zone"] == TZ_NAME
    assert pending["target_date"] == target_date.isoformat()
    _assert_no_calendar_activity(calls)


def test_chat_service_invalid_connected_timezone_is_ignored(monkeypatch):
    """An invalid Calendar timezone must not replace a valid user timezone."""
    calls, holder = _setup(monkeypatch, calendar_timezone="Mars/Olympus")
    service = _make_service(holder.make())

    _send(service, AVAILABILITY_REQUEST)
    result = calls["invoke"][-1]["result"]
    plan = result.get("gmail_plan") or {}
    assert plan.get("calendar_tool") == "check_calendar_availability"
    args = plan.get("calendar_arguments") or {}
    assert args.get("time_zone") == TZ_NAME
    target_date = _request_date(result, TZ_NAME) + timedelta(days=1)
    _assert_freebusy_calls(
        calls,
        timezone_name=TZ_NAME,
        target_date=target_date,
    )
    _assert_no_calendar_activity(calls)


def test_chat_service_full_day_resumes_pending_availability(monkeypatch):
    """Scenario B: 'check full day' resumes pending availability."""
    calls, holder = _setup(monkeypatch)
    message_service = holder.make()
    service = _make_service(message_service)

    # Turn 1 creates pending availability
    resp1, _ = _send(service, AVAILABILITY_REQUEST)
    meta1 = resp1["assistant_message"]["metadata"]
    pa1 = meta1.get("pending_calendar_availability")
    assert pa1 is not None
    assert pa1.get("whole_day") is True
    assert pa1.get("time_zone") == "Asia/Kolkata"
    assert pa1.get("target_date") is not None

    # Turn 2 resumes
    before = _tool_counts(calls)
    resp2, _ = _send(service, FULL_DAY_REQUEST)
    result = calls["invoke"][-1]["result"]
    assert result["assistant_route"] == "google_calendar"
    plan = result.get("gmail_plan") or {}
    assert plan.get("calendar_tool") == "check_calendar_availability"
    args = plan.get("calendar_arguments") or {}
    assert args.get("time_zone") == "Asia/Kolkata"
    _assert_freebusy_calls(
        calls,
        timezone_name=TZ_NAME,
        target_date=date.fromisoformat(pa1["target_date"]),
        expected_count=2,
    )
    assert len(calls["calendar_list_tool"]) == before["calendar_list_tool"] + 1
    _assert_no_calendar_activity(calls)


def test_chat_service_exact_three_step_sequence_executes_freebusy(monkeypatch):
    """The exact reported sequence must reach free/busy on all three turns."""
    calls, holder = _setup(monkeypatch)
    service = _make_service(holder.make())

    first, _ = _send(service, AVAILABILITY_REQUEST)
    _send(service, FULL_DAY_REQUEST)
    target_date = date.fromisoformat(
        first["assistant_message"]["metadata"]["pending_calendar_availability"][
            "target_date"
        ]
    )

    before = _tool_counts(calls)
    _send(service, "asia india")
    result = calls["invoke"][-1]["result"]
    assert result["assistant_route"] == "google_calendar"
    plan = result.get("gmail_plan") or {}
    assert plan.get("calendar_tool") == "check_calendar_availability"
    args = plan.get("calendar_arguments") or {}
    assert args.get("time_zone") == "Asia/Kolkata"
    _assert_freebusy_calls(
        calls,
        timezone_name=TZ_NAME,
        target_date=target_date,
        expected_count=3,
    )
    # An explicit timezone reply does not need another authoritative lookup.
    assert len(calls["calendar_list_tool"]) == before["calendar_list_tool"]
    _assert_no_calendar_activity(calls)


def test_chat_service_gmail_switch_during_availability(monkeypatch):
    """Scenario E part 1: Gmail request during availability → Gmail."""
    calls, holder = _setup(monkeypatch)
    message_service = holder.make()
    service = _make_service(message_service)

    _send(service, AVAILABILITY_REQUEST)

    before = _tool_counts(calls)
    resp, _ = _send(service, GMAIL_REQUEST)
    result = calls["invoke"][-1]["result"]
    assert result["assistant_route"] == "gmail"
    assert ASSISTANT_ROUTE_NODES["gmail"] == "gmail_agent"
    assert len(calls["gmail"]) == before["gmail"] + 1
    assert len(calls["calendar_list"]) == before["calendar_list"]
    assert len(calls["calendar_list_tool"]) == before["calendar_list_tool"]
    _assert_no_calendar_activity(calls)

    # Pending availability is suspended (not in latest metadata) but restorable
    latest_meta = resp["assistant_message"]["metadata"]
    assert "pending_calendar_availability" not in latest_meta
    history = message_service.get_conversation_messages()
    now = datetime.now(timezone.utc)
    restored = ChatService._pending_calendar_availability(
        history,
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        connection_id=str(CALENDAR_CONNECTION_ID),
        now=now,
    )
    assert restored is not None


def test_chat_service_return_to_calendar_after_gmail(monkeypatch):
    """Scenario E part 2: return to Calendar after Gmail switch."""
    calls, holder = _setup(monkeypatch)
    message_service = holder.make()
    service = _make_service(message_service)

    _send(service, AVAILABILITY_REQUEST)
    _send(service, GMAIL_REQUEST)

    before = _tool_counts(calls)
    resp, _ = _send(service, FULL_DAY_REQUEST)
    result = calls["invoke"][-1]["result"]
    assert result["assistant_route"] == "google_calendar"
    assert ASSISTANT_ROUTE_NODES["google_calendar"] == "calendar_agent"
    plan = result.get("gmail_plan") or {}
    assert plan.get("calendar_tool") == "check_calendar_availability"
    assert len(calls["calendar_list_tool"]) == before["calendar_list_tool"] + 1
    _assert_no_calendar_activity(calls)


def test_chat_service_general_during_availability(monkeypatch):
    """Scenario F: general question during availability → general answer."""
    calls, holder = _setup(monkeypatch)
    message_service = holder.make()
    service = _make_service(message_service)

    _send(service, AVAILABILITY_REQUEST)

    before = _tool_counts(calls)
    resp, _ = _send(service, GENERAL_REQUEST)
    result = calls["invoke"][-1]["result"]
    assert result["assistant_route"] == "general"
    assert ASSISTANT_ROUTE_NODES["general"] == "memory_retrieval"
    assert len(calls["gmail"]) == before["gmail"]
    assert len(calls["calendar_list"]) == before["calendar_list"]
    assert len(calls["calendar_list_tool"]) == before["calendar_list_tool"]
    _assert_no_calendar_activity(calls)

    # Pending availability remains restorable
    history = message_service.get_conversation_messages()
    now = datetime.now(timezone.utc)
    restored = ChatService._pending_calendar_availability(
        history,
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        connection_id=str(CALENDAR_CONNECTION_ID),
        now=now,
    )
    assert restored is not None


def test_chat_service_availability_wrong_user_blocked():
    """Pending availability scoped to user_id."""
    pending = {
        "version": 1,
        "operation": "availability",
        "user_id": str(USER_ID),
        "conversation_id": str(CONVERSATION_ID),
        "connection_id": str(CALENDAR_CONNECTION_ID),
        "expires_at": "2026-11-14T15:10:00+05:30",
        "original_request": AVAILABILITY_REQUEST,
        "whole_day": True,
        "time_zone": "Asia/Kolkata",
        "target_date": "2026-11-15",
    }
    history = [{"role": "assistant", "metadata": {
        "pending_calendar_availability": pending,
    }}]
    now = datetime(2026, 11, 14, 10, 0, tzinfo=TZ)

    assert ChatService._pending_calendar_availability(
        history,
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        now=now,
    ) is not None
    assert ChatService._pending_calendar_availability(
        history,
        user_id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        conversation_id=CONVERSATION_ID,
        now=now,
    ) is None


def test_chat_service_availability_wrong_conversation_blocked():
    """Pending availability scoped to conversation_id."""
    pending = {
        "version": 1,
        "operation": "availability",
        "user_id": str(USER_ID),
        "conversation_id": str(CONVERSATION_ID),
        "connection_id": str(CALENDAR_CONNECTION_ID),
        "expires_at": "2026-11-14T15:10:00+05:30",
        "original_request": AVAILABILITY_REQUEST,
        "whole_day": True,
        "time_zone": "Asia/Kolkata",
        "target_date": "2026-11-15",
    }
    history = [{"role": "assistant", "metadata": {
        "pending_calendar_availability": pending,
    }}]
    now = datetime(2026, 11, 14, 10, 0, tzinfo=TZ)

    assert ChatService._pending_calendar_availability(
        history,
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        now=now,
    ) is not None
    assert ChatService._pending_calendar_availability(
        history,
        user_id=USER_ID,
        conversation_id=uuid4(),
        now=now,
    ) is None


def test_chat_service_availability_wrong_connection_blocked():
    """Pending availability scoped to connection_id."""
    pending = {
        "version": 1,
        "operation": "availability",
        "user_id": str(USER_ID),
        "conversation_id": str(CONVERSATION_ID),
        "connection_id": str(CALENDAR_CONNECTION_ID),
        "expires_at": "2026-11-14T15:10:00+05:30",
        "original_request": AVAILABILITY_REQUEST,
        "whole_day": True,
        "time_zone": "Asia/Kolkata",
        "target_date": "2026-11-15",
    }
    history = [{"role": "assistant", "metadata": {
        "pending_calendar_availability": pending,
    }}]
    now = datetime(2026, 11, 14, 10, 0, tzinfo=TZ)

    assert ChatService._pending_calendar_availability(
        history,
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        connection_id=str(CALENDAR_CONNECTION_ID),
        now=now,
    ) is not None
    assert ChatService._pending_calendar_availability(
        history,
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        connection_id=str(uuid4()),
        now=now,
    ) is None


def test_chat_service_expired_availability_not_restored():
    """Expired pending availability is not restored."""
    pending = {
        "version": 1,
        "operation": "availability",
        "user_id": str(USER_ID),
        "conversation_id": str(CONVERSATION_ID),
        "connection_id": str(CALENDAR_CONNECTION_ID),
        "expires_at": "2026-11-14T09:00:00+05:30",
        "original_request": AVAILABILITY_REQUEST,
        "whole_day": True,
        "time_zone": "Asia/Kolkata",
        "target_date": "2026-11-15",
    }
    history = [{"role": "assistant", "metadata": {
        "pending_calendar_availability": pending,
    }}]
    now = datetime(2026, 11, 14, 10, 0, tzinfo=TZ)

    assert ChatService._pending_calendar_availability(
        history,
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        now=now,
    ) is None


def test_chat_service_streaming_availability_not_gmail(monkeypatch):
    """Streaming path also routes availability to calendar, not Gmail."""
    calls, holder = _setup(monkeypatch)
    message_service = holder.make()
    service = _make_service(message_service)

    invoke_before = len(calls["invoke"])
    stream_before = len(calls["stream"])
    gmail_before = len(calls["gmail"])

    response, _events = _send(service, AVAILABILITY_REQUEST, streaming=True)

    assert len(calls["stream"]) == stream_before + 1
    assert len(calls["invoke"]) == invoke_before
    stream_call = calls["stream"][-1]
    routes = [
        payload.get("assistant_route")
        for mode, payload in stream_call["events"]
        if mode == "values" and isinstance(payload, dict)
    ]
    assert "google_calendar" in routes
    assert ASSISTANT_ROUTE_NODES["google_calendar"] == "calendar_agent"
    assert len(calls["gmail"]) == gmail_before
    _assert_no_calendar_activity(calls)

    latest_metadata = response["assistant_message"]["metadata"]
    pa = latest_metadata.get("pending_calendar_availability")
    if pa is not None:
        assert pa.get("operation") == "availability"
