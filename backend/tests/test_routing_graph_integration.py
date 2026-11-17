import json
from contextlib import nullcontext
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest

from app.core.request_metrics import RequestMetrics, current_request_metrics
from app.services import chat_service as chat_service_module
from app.services.chat_service import ChatService
from app.workflows import calendar_agent_node
from app.workflows import contacts_read
from app.workflows import gmail_agent_nodes
from app.workflows.chat_graph import ASSISTANT_ROUTE_NODES


USER_ID = UUID("c0cd2d85-c90c-4c62-a886-c41bec1c41af")
CONVERSATION_ID = UUID("c7557e52-cd75-4ea6-9130-e927176518a8")
CAL_CONNECTION_ID = UUID("5a3d3426-f1fd-4cc9-ae83-08181848cde7")
CONTACTS_CONNECTION_ID = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")


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

    def __init__(self):
        self.contacts_read_id = str(CONTACTS_CONNECTION_ID)

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
            "event_read_connection_id": str(CAL_CONNECTION_ID),
            "freebusy_connection_id": None,
            "write_connection_id": str(CAL_CONNECTION_ID),
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
            "google_connection_available": True,
            "capabilities": {},
            "read_connection_id": self.contacts_read_id,
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


def test_graph_integration_sequential_routing_no_calendar_response_for_contacts(monkeypatch):
    service, message_service, request_repo = _make_service()

    # Calendar: generic delete with no events (request 1) and check today (request 3)
    cal_service = SimpleNamespace(
        list_events=AsyncMock(return_value={"events": [], "next_page_token": None, "calendar_id": "primary"}),
        validate_event_write_access=AsyncMock(),
        get_event=AsyncMock(),
        list_calendars=AsyncMock(return_value={"calendars": [], "next_page_token": None}),
    )
    monkeypatch.setattr(calendar_agent_node, "CalendarService", lambda _db: cal_service)
    monkeypatch.setattr(calendar_agent_node, "SessionLocal", lambda: nullcontext(object()))

    # Contacts read tool/service path used by contacts workflow
    contacts_calls = {"search": 0, "get": 0}
    mam = {
        "resource_name": "people/mam-1",
        "display_name": "Mam",
        "primary_phone": "+8801700000001",
        "phones": [{"value": "+8801700000001", "primary": True}],
    }
    rintu = {
        "resource_name": "people/rintu-1",
        "display_name": "Rintu da somnath da ref",
        "primary_phone": "+8801711111111",
        "phones": [{"value": "+8801711111111", "primary": True}],
    }

    def fake_generate_contacts_read_response(**kwargs):
        msg = (kwargs.get("message") or "").lower()
        if "mam" in msg:
            contacts_calls["search"] += 1
            return {
                "assistant_response": "Mam's phone number: +8801700000001",
                "contacts_provenance": {"outcome_status": "single_match", "verified_source": True},
                "pending_contacts_clarification": None,
            }
        if "rintu" in msg:
            contacts_calls["search"] += 1
            return {
                "assistant_response": "Rintu da somnath da ref's phone number: +8801711111111",
                "contacts_provenance": {"outcome_status": "single_match", "verified_source": True},
                "pending_contacts_clarification": None,
            }
        return {
            "assistant_response": "I couldn't find a matching contact.",
            "contacts_provenance": {"outcome_status": "no_match"},
            "pending_contacts_clarification": None,
        }

    monkeypatch.setattr(__import__("app.workflows.contacts_agent_node", fromlist=["generate_contacts_read_response"]), "generate_contacts_read_response", fake_generate_contacts_read_response)
    # avoid planner LLM ambiguity in tests
    monkeypatch.setattr(gmail_agent_nodes.llm_router, "generate", Mock(side_effect=RuntimeError("planner disabled in test")))
    monkeypatch.setattr(ChatService, "_start_heartbeat", lambda *a, **k: (None, None, None))
    monkeypatch.setattr(ChatService, "_stop_heartbeat", lambda *a, **k: None)

    graph_calls = []
    original_invoke = chat_service_module.chat_graph.invoke

    def invoke_spy(state, *args, **kwargs):
        result = original_invoke(state, *args, **kwargs)
        graph_calls.append((state, result))
        return result

    monkeypatch.setattr(chat_service_module.chat_graph, "invoke", invoke_spy)

    messages = [
        "show me mam number",
        "delete the event from todays calender",
        "check my calendar for today",
        "find the number of Rintu da somnath da ref",
    ]
    responses = []
    for content in messages:
        metrics_token = current_request_metrics.set(RequestMetrics(request_id=str(uuid4())))
        try:
            responses.append(service.send_message(
                user_id=USER_ID,
                conversation_id=CONVERSATION_ID,
                content=content,
                idempotency_key=uuid4(),
                user_timezone="America/Los_Angeles",
                default_calendar_connection_id=str(CAL_CONNECTION_ID),
                default_contacts_connection_id=str(CONTACTS_CONNECTION_ID),
                calendar_enabled=True,
                contacts_enabled=True,
                gmail_enabled=False,
                drive_enabled=False,
            ))
        finally:
            current_request_metrics.reset(metrics_token)

    assert len(graph_calls) == 4
    assert len(responses) == 4

    expected_routes = ["google_contacts", "google_calendar", "google_calendar", "google_contacts"]
    for i, ((state, result), resp, expected_route) in enumerate(zip(graph_calls, responses, expected_routes), start=1):
        assert result.get("assistant_route") == expected_route, (i, result.get("assistant_route"), result.get("assistant_response"))
        # executed agent node mapping
        assert ASSISTANT_ROUTE_NODES.get(expected_route) in {
            "contacts_agent", "calendar_agent", "memory_retrieval", "gmail_agent"
        }
        # request/response association
        assert resp["conversation_id"] == CONVERSATION_ID
        assert resp["user_message"]["content"] == messages[i - 1]
        assert resp["assistant_message"]["chat_request_id"] == resp["user_message"]["chat_request_id"]
        assert resp["assistant_message"]["role"] == "assistant"

    # req 1 and 4 must be contacts and must not return calendar no-match text/hijack
    assert "Mam" in (responses[0]["assistant_message"]["content"] or "")
    assert "+8801700000001" in (responses[0]["assistant_message"]["content"] or "")
    assert "Rintu" in (responses[3]["assistant_message"]["content"] or "")
    assert "+8801711111111" in (responses[3]["assistant_message"]["content"] or "")
    for idx in (0, 3):
        text = (responses[idx]["assistant_message"]["content"] or "").lower()
        assert "couldn't find an existing calendar event" not in text
        assert "nothing was changed" not in text or "calendar" not in text
    # req 2 and 3 are calendar-routed
    assert "calendar" in (responses[1]["assistant_message"]["content"] or "").lower() or "event" in (responses[1]["assistant_message"]["content"] or "").lower() or "today" in (responses[1]["assistant_message"]["content"] or "").lower()

    # req 2 calendar delete generic no-events invoked list_events (mocked)
    assert cal_service.list_events.await_count >= 1
    # req 4 did not get hijacked by stale calendar pending/action: contacts read executed
    assert contacts_calls["search"] >= 2
