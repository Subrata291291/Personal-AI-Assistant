import json
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

from app.core.request_metrics import RequestMetrics, current_request_metrics
from app.llm.schemas import LLMResponse
from app.services import chat_service as chat_service_module
from app.services.chat_service import ChatService
from app.workflows import gmail_agent_nodes
from app.workflows import chat_nodes


USER_ID = UUID("c0cd2d85-c90c-4c62-a886-c41bec1c41af")
CONVERSATION_ID = UUID("c7557e52-cd75-4ea6-9130-e927176518a8")
GMAIL_CONNECTION_ID = UUID("5a3d3426-f1fd-4cc9-ae83-08181848cde7")
OTHER_USER_ID = UUID("11111111-2222-3333-4444-555555555555")
OTHER_CONVERSATION_ID = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")


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

    def get_granted_google_scopes(self, **_kwargs):
        return set()

    def get_default_google_connection_for_scopes(self, **_kwargs):
        return None

    def get_user_calendar_access(self, **_kwargs):
        return {
            "google_connection_available": False,
            "capabilities": {},
            "event_read_connection_id": None,
            "freebusy_connection_id": None,
            "write_connection_id": None,
            "default_connection_invalid": False,
            "default_connection_selected": False,
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


def _ok_llm_response(text="OK", provider="test", model="test-model"):
    return LLMResponse(
        content=text,
        provider=provider,
        model=model,
        finish_reason="stop",
        latency_ms=0,
        output_limited=False,
    )


def _send(service, content, *, user_id=USER_ID, conversation_id=CONVERSATION_ID, user_timezone="Asia/Kolkata", request_time=None):
    metrics = RequestMetrics(request_id=str(uuid4()))
    # allow deterministic tests to control "now" via request_time if supported by state; ChatService uses its own request_time internally,
    # but graph/state sees request_time from service. We pass timezone and rely on service behavior.
    token = current_request_metrics.set(metrics)
    try:
        return service.send_message(
            user_id=user_id,
            conversation_id=conversation_id,
            content=content,
            idempotency_key=uuid4(),
            user_timezone=user_timezone,
            gmail_connection_id=GMAIL_CONNECTION_ID,
            gmail_enabled=True,
            calendar_enabled=False,
            contacts_enabled=False,
            drive_enabled=False,
        )
    finally:
        current_request_metrics.reset(token)


def _last_assistant_metadata(message_service):
    for m in reversed(message_service.messages):
        if m.get("role") == "assistant":
            return m.get("metadata") or {}
    return {}


def test_gmail_date_range_clarification_created_persisted_and_resolved_by_whole_day(monkeypatch):
    service, message_service, _ = _make_service()
    monkeypatch.setattr(ChatService, "_start_heartbeat", lambda *a, **k: (None, None, None))
    monkeypatch.setattr(ChatService, "_stop_heartbeat", lambda *a, **k: None)
    # planner fallback in some environments; clarification emission path in gmail_agent_node handles this case
    monkeypatch.setattr(gmail_agent_nodes.llm_router, "generate", Mock(side_effect=RuntimeError("planner disabled in test")))
    monkeypatch.setattr(chat_nodes.llm_router, "generate", Mock(return_value=_ok_llm_response("I can help with that.")))
    # mock gmail read execution for resolved whole-day search
    gmail_exec = AsyncMock(return_value={
        "messages": [],
        "search_criteria": "after:... before:...",
        "period": "whole day",
    })
    monkeypatch.setattr(gmail_agent_nodes, "execute_gmail_read", gmail_exec, raising=False)

    # Step 1: Gmail-only date-specific request emits a real Gmail date-range clarification.
    r1 = _send(service, "check my emails for tomorrow")
    assert r1["assistant_message"]["role"] == "assistant"
    text1 = (r1["assistant_message"]["content"] or "").lower()
    assert "whole day" in text1 or "start and end time" in text1
    meta1 = r1["assistant_message"].get("metadata") or {}
    pending = meta1.get("pending_gmail_clarification")
    assert isinstance(pending, dict)
    assert pending.get("type") == "gmail_date_range_clarification"
    assert pending.get("version") == 1
    assert str(pending.get("user_id")) == str(USER_ID)
    assert str(pending.get("conversation_id")) == str(CONVERSATION_ID)
    assert pending.get("expires_at")

    # Step 2: "whole day" resolves that exact pending clarification in same conversation.
    r2 = _send(service, "whole day")
    meta2 = r2["assistant_message"].get("metadata") or {}
    # cleared after successful resolution
    assert meta2.get("pending_gmail_clarification") is None
    # Gmail read execution occurred for resolved request (mocked), not a clarification-only response
    assert gmail_exec.await_count >= 1
    # sanity: not routed to calendar
    graph_state = None  # not directly exposed; assert response content is not a calendar create prompt
    assert "calendar event" not in (r2["assistant_message"]["content"] or "").lower()


def test_whole_day_without_pending_gmail_clarification_does_not_invent_one(monkeypatch):
    service, message_service, _ = _make_service()
    monkeypatch.setattr(ChatService, "_start_heartbeat", lambda *a, **k: (None, None, None))
    monkeypatch.setattr(ChatService, "_stop_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(gmail_agent_nodes.llm_router, "generate", Mock(side_effect=RuntimeError("planner disabled in test")))
    monkeypatch.setattr(chat_nodes.llm_router, "generate", Mock(return_value=_ok_llm_response("I can help with that.")))
    monkeypatch.setattr(gmail_agent_nodes, "execute_gmail_read", AsyncMock(return_value={"messages": []}), raising=False)

    r = _send(service, "whole day")
    meta = r["assistant_message"].get("metadata") or {}
    assert meta.get("pending_gmail_clarification") in (None, {})
    assert "google_calendar" != (meta.get("assistant_route") or "")


def test_unambiguous_gmail_today_request_routes_directly_to_summarize_today_service_level(monkeypatch):
    service, message_service, _ = _make_service()
    monkeypatch.setattr(ChatService, "_start_heartbeat", lambda *a, **k: (None, None, None))
    monkeypatch.setattr(ChatService, "_stop_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(gmail_agent_nodes.llm_router, "generate", Mock(side_effect=RuntimeError("planner disabled in test")))
    monkeypatch.setattr(chat_nodes.llm_router, "generate", Mock(return_value=_ok_llm_response("I can help with that.")))
    gmail_exec = AsyncMock(return_value={
        "messages": [],
        "period": "today",
        "search_criteria": "in:inbox newer_than:1d -from:me",
    })
    monkeypatch.setattr(gmail_agent_nodes, "execute_gmail_read", gmail_exec, raising=False)

    r = _send(service, "check what emails I have for today")
    meta = r["assistant_message"].get("metadata") or {}
    assert meta.get("pending_gmail_clarification") in (None, {})
    text = (r["assistant_message"]["content"] or "").lower()
    assert "start and end time" not in text
    assert "what date" not in text
    assert gmail_exec.await_count >= 1


def test_expired_gmail_clarification_cannot_be_consumed(monkeypatch):
    service, message_service, _ = _make_service()
    monkeypatch.setattr(ChatService, "_start_heartbeat", lambda *a, **k: (None, None, None))
    monkeypatch.setattr(ChatService, "_stop_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(gmail_agent_nodes.llm_router, "generate", Mock(side_effect=RuntimeError("planner disabled in test")))
    monkeypatch.setattr(chat_nodes.llm_router, "generate", Mock(return_value=_ok_llm_response("I can help with that.")))
    gmail_exec = AsyncMock(return_value={"messages": []})
    monkeypatch.setattr(gmail_agent_nodes, "execute_gmail_read", gmail_exec, raising=False)

    # Seed an expired pending clarification in history (assistant metadata).
    expired = {
        "version": 1,
        "type": "gmail_date_range_clarification",
        "user_id": str(USER_ID),
        "conversation_id": str(CONVERSATION_ID),
        "created_at": "2026-01-01T00:00:00+00:00",
        "expires_at": "2026-01-01T00:10:00+00:00",
        "period": "date",
        "target_date": "2026-11-20",
        "time_zone": "Asia/Kolkata",
    }
    message_service.messages.append({
        "id": uuid4(),
        "conversation_id": CONVERSATION_ID,
        "chat_request_id": uuid4(),
        "role": "assistant",
        "content": "Do you want emails for the whole day?",
        "metadata": {"pending_gmail_clarification": expired},
        "created_at": datetime.now(timezone.utc),
    })

    r = _send(service, "whole day")
    meta = r["assistant_message"].get("metadata") or {}
    # must not consume expired clarification; cleared/superseded deterministically
    assert meta.get("pending_gmail_clarification") in (None, {})
    # should not execute the clarified Gmail day query as a resolved continuation
    assert gmail_exec.await_count == 0


def test_other_user_cannot_consume_gmail_clarification(monkeypatch):
    service, message_service, _ = _make_service()
    monkeypatch.setattr(ChatService, "_start_heartbeat", lambda *a, **k: (None, None, None))
    monkeypatch.setattr(ChatService, "_stop_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(gmail_agent_nodes.llm_router, "generate", Mock(side_effect=RuntimeError("planner disabled in test")))
    monkeypatch.setattr(chat_nodes.llm_router, "generate", Mock(return_value=_ok_llm_response("I can help with that.")))
    gmail_exec = AsyncMock(return_value={"messages": []})
    monkeypatch.setattr(gmail_agent_nodes, "execute_gmail_read", gmail_exec, raising=False)

    valid = {
        "version": 1,
        "type": "gmail_date_range_clarification",
        "user_id": str(USER_ID),
        "conversation_id": str(CONVERSATION_ID),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
        "period": "date",
        "target_date": "2026-11-20",
        "time_zone": "Asia/Kolkata",
    }
    message_service.messages.append({
        "id": uuid4(),
        "conversation_id": CONVERSATION_ID,
        "chat_request_id": uuid4(),
        "role": "assistant",
        "content": "Do you want emails for the whole day?",
        "metadata": {"pending_gmail_clarification": valid},
        "created_at": datetime.now(timezone.utc),
    })

    r = _send(service, "whole day", user_id=OTHER_USER_ID, conversation_id=CONVERSATION_ID)
    meta = r["assistant_message"].get("metadata") or {}
    assert meta.get("pending_gmail_clarification") in (None, {})
    assert gmail_exec.await_count == 0


def test_other_conversation_cannot_consume_gmail_clarification(monkeypatch):
    service, message_service, _ = _make_service()
    monkeypatch.setattr(ChatService, "_start_heartbeat", lambda *a, **k: (None, None, None))
    monkeypatch.setattr(ChatService, "_stop_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(gmail_agent_nodes.llm_router, "generate", Mock(side_effect=RuntimeError("planner disabled in test")))
    monkeypatch.setattr(chat_nodes.llm_router, "generate", Mock(return_value=_ok_llm_response("I can help with that.")))
    gmail_exec = AsyncMock(return_value={"messages": []})
    monkeypatch.setattr(gmail_agent_nodes, "execute_gmail_read", gmail_exec, raising=False)

    valid = {
        "version": 1,
        "type": "gmail_date_range_clarification",
        "user_id": str(USER_ID),
        "conversation_id": str(CONVERSATION_ID),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
        "period": "date",
        "target_date": "2026-11-20",
        "time_zone": "Asia/Kolkata",
    }
    message_service.messages.append({
        "id": uuid4(),
        "conversation_id": CONVERSATION_ID,
        "chat_request_id": uuid4(),
        "role": "assistant",
        "content": "Do you want emails for the whole day?",
        "metadata": {"pending_gmail_clarification": valid},
        "created_at": datetime.now(timezone.utc),
    })

    r = _send(service, "whole day", user_id=USER_ID, conversation_id=OTHER_CONVERSATION_ID)
    meta = r["assistant_message"].get("metadata") or {}
    assert meta.get("pending_gmail_clarification") in (None, {})
    assert gmail_exec.await_count == 0


def test_unrelated_new_request_supersedes_gmail_clarification(monkeypatch):
    service, message_service, _ = _make_service()
    monkeypatch.setattr(ChatService, "_start_heartbeat", lambda *a, **k: (None, None, None))
    monkeypatch.setattr(ChatService, "_stop_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(gmail_agent_nodes.llm_router, "generate", Mock(side_effect=RuntimeError("planner disabled in test")))
    monkeypatch.setattr(chat_nodes.llm_router, "generate", Mock(return_value=_ok_llm_response("I can help with that.")))
    monkeypatch.setattr(gmail_agent_nodes, "execute_gmail_read", AsyncMock(return_value={"messages": []}), raising=False)

    # Create pending clarification first.
    r1 = _send(service, "check my emails for tomorrow")
    meta1 = r1["assistant_message"].get("metadata") or {}
    assert isinstance(meta1.get("pending_gmail_clarification"), dict)

    # Unrelated new request should supersede/clear it safely.
    r2 = _send(service, "what can you do?")
    meta2 = r2["assistant_message"].get("metadata") or {}
    assert meta2.get("pending_gmail_clarification") is None


def test_mixed_gmail_calendar_request_does_not_create_gmail_pending_and_clears_stale(monkeypatch):
    service, message_service, _ = _make_service()
    monkeypatch.setattr(ChatService, "_start_heartbeat", lambda *a, **k: (None, None, None))
    monkeypatch.setattr(ChatService, "_stop_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(gmail_agent_nodes.llm_router, "generate", Mock(side_effect=RuntimeError("planner disabled in test")))
    monkeypatch.setattr(chat_nodes.llm_router, "generate", Mock(return_value=_ok_llm_response("I can help with that.")))
    gmail_exec = AsyncMock(return_value={"messages": []})
    monkeypatch.setattr(gmail_agent_nodes, "execute_gmail_read", gmail_exec, raising=False)

    # Seed stale valid pending clarification.
    valid = {
        "version": 1,
        "type": "gmail_date_range_clarification",
        "user_id": str(USER_ID),
        "conversation_id": str(CONVERSATION_ID),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
        "period": "date",
        "target_date": "2026-11-20",
        "time_zone": "Asia/Kolkata",
    }
    message_service.messages.append({
        "id": uuid4(), "conversation_id": CONVERSATION_ID, "chat_request_id": uuid4(),
        "role": "assistant", "content": "Do you want emails for the whole day?",
        "metadata": {"pending_gmail_clarification": valid}, "created_at": datetime.now(timezone.utc),
    })

    r = _send(service, "show my emails and calendar events for tomorrow")
    meta = r["assistant_message"].get("metadata") or {}
    assert meta.get("pending_gmail_clarification") in (None, {})
    assert gmail_exec.await_count == 0


def test_cancel_followup_clears_pending_gmail_clarification_without_execution(monkeypatch):
    service, message_service, _ = _make_service()
    monkeypatch.setattr(ChatService, "_start_heartbeat", lambda *a, **k: (None, None, None))
    monkeypatch.setattr(ChatService, "_stop_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(gmail_agent_nodes.llm_router, "generate", Mock(side_effect=RuntimeError("planner disabled in test")))
    monkeypatch.setattr(chat_nodes.llm_router, "generate", Mock(return_value=_ok_llm_response("I can help with that.")))
    gmail_exec = AsyncMock(return_value={"messages": []})
    monkeypatch.setattr(gmail_agent_nodes, "execute_gmail_read", gmail_exec, raising=False)

    r1 = _send(service, "check my emails for tomorrow")
    meta1 = r1["assistant_message"].get("metadata") or {}
    assert isinstance(meta1.get("pending_gmail_clarification"), dict)

    r2 = _send(service, "cancel")
    meta2 = r2["assistant_message"].get("metadata") or {}
    assert meta2.get("pending_gmail_clarification") in (None, {})
    assert gmail_exec.await_count == 0


def test_malformed_pending_gmail_clarification_is_cleared_without_execution(monkeypatch):
    service, message_service, _ = _make_service()
    monkeypatch.setattr(ChatService, "_start_heartbeat", lambda *a, **k: (None, None, None))
    monkeypatch.setattr(ChatService, "_stop_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(gmail_agent_nodes.llm_router, "generate", Mock(side_effect=RuntimeError("planner disabled in test")))
    monkeypatch.setattr(chat_nodes.llm_router, "generate", Mock(return_value=_ok_llm_response("I can help with that.")))
    gmail_exec = AsyncMock(return_value={"messages": []})
    monkeypatch.setattr(gmail_agent_nodes, "execute_gmail_read", gmail_exec, raising=False)

    malformed = {
        "version": 1,
        "type": "gmail_date_range_clarification",
        "user_id": str(USER_ID),
        "conversation_id": str(CONVERSATION_ID),
        "created_at": datetime.now(timezone.utc).isoformat(),
        # missing/invalid expires_at makes it malformed for consumption
        "expires_at": "not-a-date",
        "period": "date",
        "target_date": "2026-11-20",
        "time_zone": "Asia/Kolkata",
    }
    message_service.messages.append({
        "id": uuid4(), "conversation_id": CONVERSATION_ID, "chat_request_id": uuid4(),
        "role": "assistant", "content": "Do you want emails for the whole day?",
        "metadata": {"pending_gmail_clarification": malformed}, "created_at": datetime.now(timezone.utc),
    })

    r = _send(service, "whole day")
    meta = r["assistant_message"].get("metadata") or {}
    assert meta.get("pending_gmail_clarification") in (None, {})
    assert gmail_exec.await_count == 0
