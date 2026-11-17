import asyncio
from contextlib import nullcontext
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest
from zoneinfo import ZoneInfo

from app.core.request_metrics import RequestMetrics, current_request_metrics
from app.schemas.calendar_approval import CalendarDeleteEventParameters
from app.services import chat_service as chat_service_module
from app.services.calendar_action_executor import CalendarActionExecutor
from app.services.calendar_action_identity import build_calendar_operation_identity
from app.services.chat_service import ChatService
from app.workflows import calendar_agent_node
from app.workflows import gmail_agent_nodes


USER_ID = UUID("c0cd2d85-c90c-4c62-a886-c41bec1c41af")
CONVERSATION_ID = UUID("c7557e52-cd75-4ea6-9130-e927176518a8")
CAL_CONNECTION_ID = UUID("5a3d3426-f1fd-4cc9-ae83-08181848cde7")


class FakeRequestRepository:
    def __init__(self):
        self.request_ids = []

    def create_request(self, **_kwargs):
        rid = uuid4()
        self.request_ids.append(rid)
        return {"id": rid, "status": "processing"}, True

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
            if chat_request_id is not None and message.get("chat_request_id") != chat_request_id:
                continue
            return message
        return None

    def create_request_message(self, *, conversation_id, role, content, metadata=None, chat_request_id=None, **_kwargs):
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
        return next((m for m in self.messages if m["id"] == message_id), None)

    def get_conversation_messages(self, **_kwargs):
        return list(self.messages)

    def update_request_message_content(self, *, message_id, content, metadata=None, **_kwargs):
        for m in self.messages:
            if m["id"] == message_id:
                m["content"] = content
                if metadata is not None:
                    m["metadata"] = metadata
                return m
        return None


class FakeUserConnectionService:
    db = object()

    def get_user_calendar_access(self, **_kwargs):
        return {
            "google_connection_available": True,
            "capabilities": {
                "write_permission_granted": True,
                "calendar_write_execution_available": True,
                "calendar_update_execution_available": True,
                "calendar_create_execution_available": True,
                "calendar_delete_execution_available": True,
                "calendar_update_feature_enabled": True,
            },
            "event_read_connection_id": str(CAL_CONNECTION_ID),
            "freebusy_connection_id": None,
            "write_connection_id": str(CAL_CONNECTION_ID),
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
        return {"google_connection_available": False, "capabilities": {}, "read_connection_id": None, "write_connection_id": None, "selected_connection_email": None, "default_connection_invalid": False, "default_connection_selected": False, "selection_required": False, "candidate_emails": [], "read_scope_missing": False, "write_scope_missing": False}

    def get_user_drive_access(self, **_kwargs):
        return {"google_connection_available": False, "capabilities": {}, "connection_id": None, "selected_connection_email": None, "default_connection_invalid": False, "default_connection_selected": False, "selection_required": False, "candidate_emails": [], "read_scope_missing": False, "enabled": False}


def _make_service():
    message_service = FakeMessageService()
    request_repository = FakeRequestRepository()
    user_connection_service = FakeUserConnectionService()
    service = ChatService(
        message_service=message_service,
        user_settings_service=SimpleNamespace(get_settings=Mock(return_value=SimpleNamespace(memory_enabled=False))),
        memory_pipeline=SimpleNamespace(process=Mock(return_value=None)),
        request_repository=request_repository,
        user_connection_service=user_connection_service,
    )
    return service, message_service, request_repository


def _event(event_id, summary, start_dt, end_dt, tz_name="America/Los_Angeles"):
    tz = ZoneInfo(tz_name)
    return {
        "id": event_id,
        "summary": summary,
        "status": "confirmed",
        "etag": f'"etag-{event_id}"',
        "start": {"date_time": start_dt, "time_zone": tz_name},
        "end": {"date_time": end_dt, "time_zone": tz_name},
        "attendees": [],
        "description": None,
        "location": None,
        "html_link": f"https://calendar.google.com/event?eid={event_id}",
        "recurrence": None,
    }


def test_chat_graph_generic_delete_select_second_creates_trusted_delete_proposal(monkeypatch):
    """ChatService+graph: generic delete -> list -> 'delete the second one' creates correct proposal (no execution)."""
    service, message_service, _req_repo = _make_service()
    tz = ZoneInfo("America/Los_Angeles")
    e1 = _event("evt-1", "Team Meeting", datetime(2026, 11, 16, 10, 0, tzinfo=tz), datetime(2026, 11, 16, 11, 0, tzinfo=tz))
    e2 = _event("evt-2", "Client Call", datetime(2026, 11, 16, 15, 0, tzinfo=tz), datetime(2026, 11, 16, 15, 30, tzinfo=tz))

    cal_service = SimpleNamespace(
        list_events=AsyncMock(return_value={"events": [e1, e2], "next_page_token": None, "calendar_id": "primary"}),
        get_event=AsyncMock(side_effect=lambda **kw: {"evt-1": e1, "evt-2": e2}[kw["event_id"]]),
        validate_event_write_access=AsyncMock(side_effect=lambda **kw: {"evt-1": e1, "evt-2": e2}[kw["event_id"]]),
        list_calendars=AsyncMock(return_value={"calendars": [], "next_page_token": None}),
    )
    monkeypatch.setattr(calendar_agent_node, "CalendarService", lambda _db: cal_service)
    monkeypatch.setattr(calendar_agent_node, "SessionLocal", lambda: nullcontext(object()))
    monkeypatch.setattr(gmail_agent_nodes.llm_router, "generate", Mock(side_effect=RuntimeError("planner disabled in test")))
    monkeypatch.setattr(ChatService, "_start_heartbeat", lambda *a, **k: (None, None, None))
    monkeypatch.setattr(ChatService, "_stop_heartbeat", lambda *a, **k: None)

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
                    "parameters": payload.parameters,
                    "connection_id": str(payload.connection_id),
                    "user_id": str(USER_ID),
                    "conversation_id": str(CONVERSATION_ID),
                }
            )

    monkeypatch.setattr(chat_service_module, "ActionApprovalService", FakeActionApprovalService)

    graph_calls = []
    original_invoke = chat_service_module.chat_graph.invoke

    def invoke_spy(state, *args, **kwargs):
        result = original_invoke(state, *args, **kwargs)
        graph_calls.append((state, result))
        return result

    monkeypatch.setattr(chat_service_module.chat_graph, "invoke", invoke_spy)

    msgs = [
        "delete the event from todays calender",
        "delete the second one",
    ]
    responses = []
    saved_counts = []
    for content in msgs:
        metrics_token = current_request_metrics.set(RequestMetrics(request_id=str(uuid4())))
        try:
            responses.append(service.send_message(
                user_id=USER_ID,
                conversation_id=CONVERSATION_ID,
                content=content,
                idempotency_key=uuid4(),
                user_timezone="America/Los_Angeles",
                default_calendar_connection_id=str(CAL_CONNECTION_ID),
                calendar_enabled=True,
                contacts_enabled=False,
                gmail_enabled=False,
                drive_enabled=False,
            ))
        finally:
            current_request_metrics.reset(metrics_token)
        saved_counts.append(len(saved_proposals))

    assert len(graph_calls) == 2
    # Request 1: multiple events listed, trusted selection stored, no proposal
    r1_meta = responses[0]["assistant_message"].get("metadata") or {}
    assert r1_meta.get("pending_calendar_delete_selection") is not None
    candidates = r1_meta["pending_calendar_delete_selection"]["candidates"]
    assert [c["event_id"] for c in candidates] == ["evt-1", "evt-2"]
    assert saved_counts[0] == 0
    assert "action_approval" not in r1_meta

    # Request 2: ordinal selection resolves to second event and creates trusted delete proposal (no execution)
    assert saved_counts[1] == 1
    _pid, payload = saved_proposals[0]
    assert payload.action_type == "calendar.delete_event"
    assert payload.parameters["event_id"] == "evt-2"
    assert payload.parameters["calendar_id"] == "primary"
    assert payload.connection_id == CAL_CONNECTION_ID

    r2 = responses[1]
    r2_meta = r2["assistant_message"].get("metadata") or {}
    assert r2["assistant_message"]["chat_request_id"] == r2["user_message"]["chat_request_id"]
    assert graph_calls[1][1].get("assistant_route") == "google_calendar"
    assert r2_meta.get("action_approval", {}).get("action_type") == "calendar.delete_event"
    assert r2_meta.get("action_approval", {}).get("parameters", {}).get("event_id") == "evt-2"
    # No direct delete API call in chat/agent/orchestration path (execution is separate existing approval mechanism)
    assert cal_service.validate_event_write_access.await_count >= 1
    # CalendarService in this mocked path has no delete mutation method; executor performs DELETE separately after approval.


def _delete_executor(monkeypatch, event_id="evt-2", calendar_id="primary", etag='"etag-evt-2"'):
    monkeypatch.setattr("app.services.calendar_action_executor.settings.CALENDAR_DELETE_EVENT_ENABLED", True)
    parameters = CalendarDeleteEventParameters.model_validate({
        "calendar_id": calendar_id,
        "event_id": event_id,
        "expected_etag": etag,
        "current_event": {
            "summary": "Client Call",
            "description": None,
            "location": None,
            "start": {"dateTime": "2026-11-16T15:00:00-08:00", "timeZone": "America/Los_Angeles"},
            "end": {"dateTime": "2026-11-16T15:30:00-08:00", "timeZone": "America/Los_Angeles"},
            "attendees": [],
            "etag": etag,
        },
    }).model_dump(mode="json")
    identity = build_calendar_operation_identity(
        proposal_id=UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),
        user_id=USER_ID,
        connection_id=CAL_CONNECTION_ID,
        action_type="calendar.delete_event",
        parameters=parameters,
    )
    proposal = SimpleNamespace(
        proposal_id=UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),
        action_type="calendar.delete_event",
        status="approved",
        parameters=parameters,
        connection_id=CAL_CONNECTION_ID,
        operation_id=identity.operation_id,
        payload_fingerprint=identity.payload_fingerprint,
        calendar_event_id=identity.event_id,
        execution_started_at=None,
    )
    completed = SimpleNamespace(status="completed", result={"outcome": "deleted", "google_event_id": event_id})
    approval = SimpleNamespace(
        get_proposal=Mock(return_value=proposal),
        claim_calendar_execution=Mock(return_value=proposal),
        finish_calendar_execution=Mock(return_value=completed),
        calendar_operation_repository=Mock(),
    )
    calendar = SimpleNamespace(
        validate_event_write_access=AsyncMock(return_value={
            "id": event_id, "summary": "Client Call", "description": None, "location": None,
            "etag": etag,
            "start": {"date": None, "date_time": datetime(2026, 11, 16, 23, 0, tzinfo=timezone.utc), "time_zone": "America/Los_Angeles"},
            "end": {"date": None, "date_time": datetime(2026, 11, 16, 23, 30, tzinfo=timezone.utc), "time_zone": "America/Los_Angeles"},
            "attendees": [],
        }),
        _google_get=AsyncMock(),
        _normalize_event=Mock(),
    )
    api = SimpleNamespace(request_mutation=AsyncMock(return_value={}))
    monkeypatch.setattr("app.services.calendar_action_executor.SessionLocal", lambda: SimpleNamespace(close=Mock()))
    executor = CalendarActionExecutor(db=None, approval_service=approval, calendar_service=calendar, api_client=api)
    executor._read_event_for_reconciliation = AsyncMock(return_value=None)  # deleted/absent confirms success
    return executor, approval, api, proposal, completed


def test_existing_approval_execution_calls_delete_with_verified_selected_event_ids(monkeypatch):
    """Existing approval+executor mechanism deletes the exact selected event_id/calendar_id after approval (mocked Google API)."""
    executor, approval, api, _proposal, completed = _delete_executor(monkeypatch, event_id="evt-2", calendar_id="primary")

    result = asyncio.run(executor.execute(user_id=USER_ID, proposal_id=UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")))

    assert result is completed
    # No delete before approval/execution claim: claim is part of executor flow after approval state checks
    approval.claim_calendar_execution.assert_called_once()
    approval.get_proposal.assert_called()
    assert api.request_mutation.await_count == 1
    req = api.request_mutation.await_args.kwargs
    assert req["method"] == "DELETE"
    assert "/calendars/primary/events/evt-2" in req["url"]
    assert req["headers"].get("If-Match") == '"etag-evt-2"'
    finish = approval.finish_calendar_execution.call_args.kwargs
    assert finish["state"] == "completed"
    assert finish["safe_result"]["outcome"] == "deleted"
    assert finish["safe_result"]["google_event_id"] == "evt-2"
