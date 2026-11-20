import asyncio
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest

from app.core.request_metrics import RequestMetrics, current_request_metrics
from app.schemas.calendar_approval import CalendarCreateEventParameters
from app.services import calendar_action_identity
from app.services import calendar_action_executor
from app.services import chat_service as chat_service_module
from app.services.calendar_action_executor import CalendarActionExecutor
from app.services.chat_service import ChatService
from app.workflows import calendar_agent_node
from app.workflows import gmail_agent_nodes
from app.workflows.calendar_agent_node import calendar_agent_node as run_calendar_node


USER_ID = "c0cd2d85-c90c-4c62-a886-c41bec1c41af"
CONVERSATION_ID = "c7557e52-cd75-4ea6-9130-e927176518a8"
CONNECTION_ID = UUID("5a3d3426-f1fd-4cc9-ae83-08181848cde7")
OTHER_CONNECTION_ID = UUID("20f915fe-3cc3-4860-888f-aa079789c322")
TRUSTED_EVENT_ID = "pa0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"


def _event(event_id=TRUSTED_EVENT_ID):
    return {
        "id": event_id,
        "summary": "Gym time",
        "description": None,
        "location": None,
        "start": {
            "date_time": datetime.fromisoformat("2026-11-21T08:00:00+05:30"),
            "date": None,
            "time_zone": "Asia/Kolkata",
        },
        "end": {
            "date_time": datetime.fromisoformat("2026-11-21T09:00:00+05:30"),
            "date": None,
            "time_zone": "Asia/Kolkata",
        },
        "attendees": [],
        "etag": f'"etag-{event_id}"',
    }


def _completed_create(status="completed", *, user_id=USER_ID, result=None):
    return {
        "proposal_id": str(uuid4()),
        "user_id": str(user_id),
        "conversation_id": CONVERSATION_ID,
        "connection_id": str(CONNECTION_ID),
        "action_type": "calendar.create_event",
        "status": status,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "result": (
            {"outcome": "created", "google_event_id": TRUSTED_EVENT_ID}
            if result is None
            else result
        ),
        "parameters": {
            "calendar_id": "primary",
            "summary": "Gym time",
            "start": "2026-11-21T08:00:00+05:30",
            "end": "2026-11-21T09:00:00+05:30",
            "time_zone": "Asia/Kolkata",
        },
    }


def _state(message, *, proposal=None):
    state = {
        "user_message": message,
        "gmail_plan": {
            "intent": "google_calendar",
            "calendar_tool": "delete_calendar_event_proposal",
            "calendar_arguments": {},
        },
        "user_id": USER_ID,
        "conversation_id": CONVERSATION_ID,
        "user_timezone": "Asia/Kolkata",
        "request_time": datetime.now(timezone.utc).isoformat(),
        "assistant_response": "",
        "provider": "",
        "model": "",
        "messages": [],
        "assistant_capabilities": {
            "google_calendar": {"write_permission_granted": True},
        },
    }
    if proposal is not None:
        state["messages"] = [
            {
                "role": "assistant",
                "content": "Created Gym time.",
                "metadata": {"action_approval": proposal},
            },
        ]
    return state


def _runtime(**overrides):
    context = {
        "user_id": USER_ID,
        "calendar_event_connection_id": str(CONNECTION_ID),
        "calendar_write_connection_id": str(CONNECTION_ID),
        "google_connection_available": True,
        "calendar_enabled": True,
    }
    context.update(overrides)
    return SimpleNamespace(context=context)


def _mock_calendar_service(monkeypatch, *, events=None, event=None, validate_error=None):
    service = SimpleNamespace(
        list_events=AsyncMock(return_value={"events": events or []}),
        list_calendars=AsyncMock(return_value={"calendars": [], "next_page_token": None}),
        get_event=AsyncMock(return_value=event or _event()),
        validate_event_write_access=(
            AsyncMock(side_effect=validate_error)
            if validate_error is not None
            else AsyncMock(return_value=event or _event())
        ),
    )
    monkeypatch.setattr(calendar_agent_node, "CalendarService", lambda _db: service)
    monkeypatch.setattr(
        calendar_agent_node, "SessionLocal", lambda: nullcontext(object())
    )
    return service


@pytest.mark.parametrize(
    "message",
    (
        "ok delete this from the calendar",
        "delete that event",
        "remove the event I just created",
        "delete Gym time from google calendar",
        "delete Gym time 8am to 9am",
        "Remove the last event we created",
    ),
)
def test_contextual_delete_phrases_resolve_trusted_event_id(monkeypatch, message):
    service = _mock_calendar_service(monkeypatch)

    result = run_calendar_node(
        _state(message, proposal=_completed_create()),
        _runtime(),
    )

    assert "calendar_action_proposal" in result
    assert result["calendar_action_proposal"]["parameters"]["event_id"] == (
        TRUSTED_EVENT_ID
    )
    service.validate_event_write_access.assert_awaited()
    # A proposal is created; execution still requires approval and a later step.
    assert "calendar_execution_result" not in result


def test_exact_title_date_time_lookup_uses_authoritative_api_data(monkeypatch):
    service = _mock_calendar_service(monkeypatch, events=[_event("api-event-id")])

    result = run_calendar_node(
        _state(
            "Delete my event titled Gym time on November 21, 2026 from 8am to 9am."
        ),
        _runtime(),
    )

    assert "calendar_action_proposal" in result
    assert result["calendar_action_proposal"]["parameters"]["event_id"] == (
        "api-event-id"
    )
    service.list_events.assert_awaited()


def test_other_user_context_cannot_supply_trusted_event_id():
    proposal = _completed_create(user_id="another-user")

    assert calendar_agent_node._recent_created_calendar_event(
        _state("delete this from the calendar", proposal=proposal)
    ) == (None, None)


def test_other_connection_cannot_validate_the_event(monkeypatch):
    _mock_calendar_service(
        monkeypatch,
        validate_error=PermissionError("wrong connection"),
    )

    result = run_calendar_node(
        _state("delete this from the calendar", proposal=_completed_create()),
        _runtime(calendar_write_connection_id=str(OTHER_CONNECTION_ID)),
    )

    assert "calendar_action_proposal" not in result
    assert "Nothing was changed" in result["assistant_response"]


def test_nonexistent_event_returns_safe_message(monkeypatch):
    _mock_calendar_service(monkeypatch, events=[])

    result = run_calendar_node(
        _state("Delete my event titled Gym time on November 21, 2026."),
        _runtime(),
    )

    assert "calendar_action_proposal" not in result
    assert "couldn’t find" in result["assistant_response"].casefold()


def test_multiple_matching_events_trigger_clarification(monkeypatch):
    _mock_calendar_service(
        monkeypatch,
        events=[_event("event-one"), _event("event-two")],
    )

    result = run_calendar_node(
        _state("Delete my event titled Gym time on November 21, 2026."),
        _runtime(),
    )

    assert "calendar_action_proposal" not in result
    assert "multiple possible" in result["assistant_response"].casefold()


def test_unexecuted_creation_is_not_trusted_as_existing_event():
    pending = _completed_create(status="pending_approval")

    assert calendar_agent_node._recent_created_calendar_event(
        _state("delete this from the calendar", proposal=pending)
    ) == (None, None)


def test_successful_selection_clears_pending_delete_state():
    pending_selection = {
        "version": 1,
        "operation": "delete_selection",
        "user_id": USER_ID,
        "conversation_id": CONVERSATION_ID,
        "connection_id": str(CONNECTION_ID),
        "calendar_id": "primary",
        "candidates": [
            {
                "event_id": "selected-event",
                "calendar_id": "primary",
                "summary": "Gym time",
                "start": "2026-11-21T08:00:00+05:30",
                "end": "2026-11-21T09:00:00+05:30",
                "time_zone": "Asia/Kolkata",
            },
        ],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": (
            datetime.now(timezone.utc) + timedelta(minutes=10)
        ).isoformat(),
        "original_request": "Delete Gym time from Google Calendar.",
    }

    routed = gmail_agent_nodes.gmail_intent_router_node(
        {
            **_state("Gym time 8am to 9am"),
            "pending_calendar_delete_selection": pending_selection,
        }
    )

    assert routed["assistant_route"] == "google_calendar"
    assert routed["gmail_plan"]["calendar_tool"] == (
        "delete_calendar_event_proposal"
    )
    assert routed["gmail_plan"]["calendar_arguments"]["event_id"] == (
        "selected-event"
    )
    assert routed["pending_calendar_delete_selection"] is None


def test_delete_feature_flag_disabled_blocks_execution(monkeypatch):
    monkeypatch.setattr(
        calendar_action_executor.settings,
        "CALENDAR_DELETE_EVENT_ENABLED",
        False,
    )
    approval_service = SimpleNamespace(
        get_proposal=lambda _user_id, _proposal_id: SimpleNamespace(
            action_type="calendar.delete_event",
        ),
        calendar_operation_repository=object(),
    )
    executor = CalendarActionExecutor(
        db=object(),
        approval_service=approval_service,
        calendar_service=SimpleNamespace(),
        api_client=SimpleNamespace(),
    )

    with pytest.raises(PermissionError, match="currently disabled"):
        asyncio.run(
            executor.execute(
                user_id=UUID(USER_ID),
                proposal_id=uuid4(),
            )
        )


def test_create_uses_authoritative_google_event_id_in_request_body():
    parameters = CalendarCreateEventParameters(
        calendar_id="primary",
        summary="Gym time",
        start="2026-11-21T08:00:00+05:30",
        end="2026-11-21T09:00:00+05:30",
        time_zone="Asia/Kolkata",
        attendees=[],
        all_day=False,
    )
    identity = calendar_action_identity.build_calendar_operation_identity(
        proposal_id=uuid4(),
        user_id=UUID(USER_ID),
        connection_id=CONNECTION_ID,
        action_type="calendar.create_event",
        parameters=calendar_action_executor._calendar_identity_parameters(
            parameters
        ),
    )

    body = CalendarActionExecutor._event_body(identity, parameters)

    assert body["id"] == identity.event_id
    assert body["id"].startswith("pa")
    assert body["summary"] == "Gym time"


def test_hydration_restores_completed_create_result(monkeypatch):
    service, _ = _make_chat_service()
    proposal = _completed_create()
    message = {
        "id": uuid4(),
        "role": "assistant",
        "content": "Created Gym time.",
        "metadata": {"action_approval": {"proposal_id": proposal["proposal_id"]}},
    }
    service.user_connection_service.db = object()

    class FakeApprovalService:
        def __init__(self, _db):
            pass

        def get_proposal(self, _user_id, _proposal_id):
            return SimpleNamespace(
                model_dump=lambda mode="json": proposal,
            )

    monkeypatch.setattr(
        chat_service_module, "ActionApprovalService", FakeApprovalService
    )

    hydrated = service._hydrate_action_approval_metadata(
        [message],
        user_id=UUID(USER_ID),
        conversation_id=UUID(CONVERSATION_ID),
    )

    approval = hydrated[0]["metadata"]["action_approval"]
    assert approval["status"] == "completed"
    assert approval["result"]["google_event_id"] == TRUSTED_EVENT_ID


def test_chat_service_projects_completed_metadata_into_graph_state(monkeypatch):
    service, message_service = _make_chat_service()
    proposal = _completed_create()
    message_service.messages.extend([
        {
            "id": uuid4(),
            "conversation_id": UUID(CONVERSATION_ID),
            "role": "user",
            "content": "Create Gym time tomorrow 8am to 9am.",
            "metadata": {},
            "created_at": datetime.now(timezone.utc),
        },
        {
            "id": uuid4(),
            "conversation_id": UUID(CONVERSATION_ID),
            "role": "assistant",
            "content": "Created Gym time.",
            "metadata": {
                "action_approval": {"proposal_id": proposal["proposal_id"]},
            },
            "created_at": datetime.now(timezone.utc),
        },
    ])
    service.user_connection_service.db = object()
    captured_states = []

    class FakeApprovalService:
        def __init__(self, _db):
            pass

        def get_proposal(self, _user_id, _proposal_id):
            return SimpleNamespace(model_dump=lambda mode="json": proposal)

    monkeypatch.setattr(
        chat_service_module, "ActionApprovalService", FakeApprovalService
    )

    def fake_invoke(state, *_args, **_kwargs):
        captured_states.append(state)
        return {
            "assistant_response": "I will check the Calendar event.",
            "assistant_route": "google_calendar",
            "gmail_plan": {"intent": "google_calendar"},
            "provider": "test",
            "model": "test-model",
        }

    monkeypatch.setattr(chat_service_module.chat_graph, "invoke", fake_invoke)
    monkeypatch.setattr(
        ChatService, "_start_heartbeat", lambda _self, *_a, **_k: (None, None, None)
    )
    monkeypatch.setattr(ChatService, "_stop_heartbeat", lambda _self, **_k: None)

    metrics_token = current_request_metrics.set(RequestMetrics(request_id=str(uuid4())))
    try:
        service.send_message(
            user_id=UUID(USER_ID),
            conversation_id=UUID(CONVERSATION_ID),
            content="delete this from the calendar",
            idempotency_key=uuid4(),
            user_timezone="Asia/Kolkata",
            default_calendar_connection_id=str(CONNECTION_ID),
            calendar_enabled=True,
        )
    finally:
        current_request_metrics.reset(metrics_token)

    assert captured_states
    approvals = [
        item["metadata"]["action_approval"]
        for item in captured_states[-1]["messages"]
        if isinstance(item.get("metadata"), dict)
        and isinstance(item["metadata"].get("action_approval"), dict)
    ]
    assert approvals
    assert approvals[-1]["status"] == "completed"
    assert approvals[-1]["result"]["google_event_id"] == TRUSTED_EVENT_ID


class _FakeRequestRepository:
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


class _FakeMessageService:
    def __init__(self):
        self.messages = []

    def verify_conversation_ownership(self, **_kwargs):
        return True

    def get_request_message(self, *, role, chat_request_id=None, **_kwargs):
        return next(
            (
                message
                for message in self.messages
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


class _FakeUserConnectionService:
    db = object()

    def get_user_calendar_access(self, **_kwargs):
        return {
            "google_connection_available": True,
            "capabilities": {
                "write_permission_granted": True,
                "calendar_write_execution_available": True,
                "calendar_update_execution_available": True,
                "calendar_create_execution_available": False,
                "calendar_delete_execution_available": True,
                "calendar_update_feature_enabled": True,
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

    def get_user_contacts_access(self, **_kwargs):
        return {
            "google_connection_available": False,
            "capabilities": {},
            "read_connection_id": None,
            "write_connection_id": None,
            "selected_connection_email": None,
            "default_connection_invalid": False,
            "default_connection_selected": False,
            "selection_required": False,
            "candidate_emails": [],
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
            "write_scope_missing": False,
            "enabled": False,
        }


def _make_chat_service():
    message_service = _FakeMessageService()
    service = ChatService(
        message_service=message_service,
        user_settings_service=SimpleNamespace(
            get_settings=Mock(return_value=SimpleNamespace(memory_enabled=False))
        ),
        memory_pipeline=SimpleNamespace(process=Mock(return_value=None)),
        request_repository=_FakeRequestRepository(),
        user_connection_service=_FakeUserConnectionService(),
    )
    return service, message_service
