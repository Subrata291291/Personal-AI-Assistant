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
from app.workflows.chat_graph import chat_graph


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


def _patch_common(monkeypatch):
    monkeypatch.setattr(ChatService, "_start_heartbeat", lambda *a, **k: (None, None, None))
    monkeypatch.setattr(ChatService, "_stop_heartbeat", lambda *a, **k: None)
    # Force deterministic intent routing when planner would otherwise be needed.
    monkeypatch.setattr(gmail_agent_nodes.llm_router, "generate", Mock(side_effect=RuntimeError("planner disabled in test")))
    monkeypatch.setattr(chat_nodes.llm_router, "generate", Mock(return_value=_ok_llm_response("General response.")))
    gmail_exec = AsyncMock(return_value={"messages": []})
    monkeypatch.setattr(gmail_agent_nodes, "execute_gmail_read", gmail_exec, raising=False)
    return gmail_exec


def _send(service, content, *, user_id=USER_ID, conversation_id=CONVERSATION_ID, user_timezone="Asia/Kolkata"):
    metrics = RequestMetrics(request_id=str(uuid4()))
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


def _seed_pending(message_service, pending, *, conversation_id=CONVERSATION_ID):
    message_service.messages.append({
        "id": uuid4(),
        "conversation_id": conversation_id,
        "chat_request_id": uuid4(),
        "role": "assistant",
        "content": "Would you like me to search your unread Inbox emails instead?",
        "metadata": {"pending_gmail_clarification": pending},
        "created_at": datetime.now(timezone.utc),
    })


def _fallback_pending(*, user_id=USER_ID, conversation_id=CONVERSATION_ID):
    now = datetime.now(timezone.utc)
    return {
        "version": 1,
        "type": "gmail_search_fallback_clarification",
        "user_id": str(user_id),
        "conversation_id": str(conversation_id),
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=10)).isoformat(),
        "from_intent": "summarize_important",
        "fallback_intent": "summarize_unread",
        "fallback_query": "is:unread in:inbox",
        "original_request": "Help me summarize my important emails.",
    }


def _important_summary_request_result():
    # route via deterministic planner path for "important emails"
    return chat_graph.invoke(
        {
            "user_id": str(USER_ID),
            "conversation_id": str(CONVERSATION_ID),
            "user_message": "Help me summarize my important emails.",
            "assistant_response": "",
            "provider": "",
            "model": "",
            "messages": [],
            "user_timezone": "Asia/Kolkata",
            "request_time": datetime.now(timezone.utc).isoformat(),
            "assistant_capabilities": {},
            "memory_enabled": False,
        },
        config={"configurable": {"thread_id": str(uuid4())}},
        context={
            "user_id": str(USER_ID),
            "gmail_connection_id": str(GMAIL_CONNECTION_ID),
            "gmail_enabled": True,
            "user_timezone": "Asia/Kolkata",
            "request_time": datetime.now(timezone.utc).isoformat(),
            "google_connection_available": True,
            "calendar_enabled": False,
            "contacts_enabled": False,
            "drive_enabled": False,
        },
    )


def test_summarize_important_empty_result_does_not_substitute_unread(monkeypatch):
    service, message_service, _ = _make_service()
    gmail_exec = _patch_common(monkeypatch)
    gmail_exec.return_value = {
        "messages": [],
        "search_criteria": "is:important",
        "period": "unspecified (all dates)",
        "scope": "all",
        "criteria": {
            "importance": "gmail_important",
            "read_status": "any",
            "scope": "all",
            "period": "any",
            "period_label": None,
            "window_days": None,
            "sender": None,
            "recipient": None,
            "topic": None,
        },
    }

    r = _send(service, "Help me summarize my important emails.")
    text = r["assistant_message"]["content"] or ""
    meta = r["assistant_message"].get("metadata") or {}
    pending = meta.get("pending_gmail_clarification")
    assert "didn't find any important emails" in text.lower()
    assert "gmail query: is:important" in text.lower()
    assert "did not substitute unread" in text.lower()
    assert "search your unread inbox emails instead?" not in text.lower()
    assert pending is None
    assert gmail_exec.await_count >= 1


def test_affirmative_yes_resolves_fallback_to_unread_search_and_clears_pending(monkeypatch):
    service, message_service, _ = _make_service()
    gmail_exec = _patch_common(monkeypatch)
    _seed_pending(message_service, _fallback_pending())
    gmail_exec.return_value = {
        "messages": [{"subject": "Unread mail", "sender": "a@example.com", "date": "today", "snippet": "hi"}],
        "search_criteria": "is:unread in:inbox",
    }

    r = _send(service, "yes")
    meta = r["assistant_message"].get("metadata") or {}
    assert meta.get("pending_gmail_clarification") in (None, {})
    # One Gmail execution for the alternative unread search/summarization.
    assert gmail_exec.await_count == 1
    called_intent = gmail_exec.await_args.kwargs.get("intent") or (gmail_exec.await_args.args[0] if gmail_exec.await_args.args else None)
    called_plan = gmail_exec.await_args.kwargs.get("plan") or (gmail_exec.await_args.args[1] if len(gmail_exec.await_args.args) > 1 else None)
    assert called_intent == "summarize_unread" or (isinstance(called_plan, dict) and called_plan.get("intent") == "summarize_unread")


def test_natural_confirmation_resolves_fallback(monkeypatch):
    service, message_service, _ = _make_service()
    gmail_exec = _patch_common(monkeypatch)
    _seed_pending(message_service, _fallback_pending())
    gmail_exec.return_value = {"messages": [], "search_criteria": "is:unread in:inbox"}

    r = _send(service, "Sure, go ahead")
    meta = r["assistant_message"].get("metadata") or {}
    assert meta.get("pending_gmail_clarification") in (None, {})
    assert gmail_exec.await_count == 1


def test_negative_confirmation_clears_pending_without_search(monkeypatch):
    service, message_service, _ = _make_service()
    gmail_exec = _patch_common(monkeypatch)
    _seed_pending(message_service, _fallback_pending())

    r = _send(service, "No")
    meta = r["assistant_message"].get("metadata") or {}
    assert meta.get("pending_gmail_clarification") in (None, {})
    assert gmail_exec.await_count == 0
    assert "won’t search your unread Inbox emails" in (r["assistant_message"]["content"] or "")


def test_unrelated_request_supersedes_fallback_without_executing_it(monkeypatch):
    service, message_service, _ = _make_service()
    gmail_exec = _patch_common(monkeypatch)
    _seed_pending(message_service, _fallback_pending())

    r = _send(service, "what can you do?")
    meta = r["assistant_message"].get("metadata") or {}
    assert meta.get("pending_gmail_clarification") in (None, {})
    # Fallback unread search must not run.
    assert gmail_exec.await_count == 0


def test_yes_without_pending_does_not_invent_gmail_fallback(monkeypatch):
    service, message_service, _ = _make_service()
    gmail_exec = _patch_common(monkeypatch)

    r = _send(service, "yes")
    meta = r["assistant_message"].get("metadata") or {}
    assert meta.get("pending_gmail_clarification") in (None, {})
    assert gmail_exec.await_count == 0


def test_user_message_integrity_yes_is_stored_and_returned_exactly(monkeypatch):
    service, message_service, _ = _make_service()
    _patch_common(monkeypatch)
    _seed_pending(message_service, _fallback_pending())

    r = _send(service, "yes")
    assert r["user_message"]["content"] == "yes"
    # persisted user message should be exactly "yes"
    stored_user = [m for m in message_service.messages if m.get("role") == "user" and m.get("chat_request_id") == r["user_message"].get("chat_request_id")]
    assert stored_user and stored_user[-1]["content"] == "yes"


def test_other_user_cannot_consume_fallback_clarification(monkeypatch):
    service, message_service, _ = _make_service()
    gmail_exec = _patch_common(monkeypatch)
    _seed_pending(message_service, _fallback_pending(user_id=USER_ID, conversation_id=CONVERSATION_ID), conversation_id=CONVERSATION_ID)

    # Different user sends "yes" in same conversation id context of fake ownership allows in this harness,
    # but pending payload user_id mismatch must prevent consumption as fallback.
    r = _send(service, "yes", user_id=OTHER_USER_ID, conversation_id=CONVERSATION_ID)
    meta = r["assistant_message"].get("metadata") or {}
    assert meta.get("pending_gmail_clarification") in (None, {})
    assert gmail_exec.await_count == 0


def test_other_conversation_cannot_consume_fallback_clarification(monkeypatch):
    service, message_service, _ = _make_service()
    gmail_exec = _patch_common(monkeypatch)
    # Pending belongs to original conversation; request occurs in a different conversation.
    _seed_pending(message_service, _fallback_pending(user_id=USER_ID, conversation_id=CONVERSATION_ID), conversation_id=CONVERSATION_ID)

    r = _send(service, "yes", user_id=USER_ID, conversation_id=OTHER_CONVERSATION_ID)
    meta = r["assistant_message"].get("metadata") or {}
    assert meta.get("pending_gmail_clarification") in (None, {})
    assert gmail_exec.await_count == 0


def test_confirmation_please_do_resolves_fallback(monkeypatch):
    service, message_service, _ = _make_service()
    gmail_exec = _patch_common(monkeypatch)
    _seed_pending(message_service, _fallback_pending())
    gmail_exec.return_value = {"messages": [], "search_criteria": "is:unread in:inbox"}

    r = _send(service, "Please do.")
    meta = r["assistant_message"].get("metadata") or {}
    assert meta.get("pending_gmail_clarification") in (None, {})
    assert gmail_exec.await_count == 1


def test_confirmation_search_unread_emails_resolves_fallback(monkeypatch):
    service, message_service, _ = _make_service()
    gmail_exec = _patch_common(monkeypatch)
    _seed_pending(message_service, _fallback_pending())
    gmail_exec.return_value = {"messages": [], "search_criteria": "is:unread in:inbox"}

    r = _send(service, "Search unread emails")
    meta = r["assistant_message"].get("metadata") or {}
    assert meta.get("pending_gmail_clarification") in (None, {})
    assert gmail_exec.await_count == 1


def test_negative_not_now_clears_pending_without_search(monkeypatch):
    service, message_service, _ = _make_service()
    gmail_exec = _patch_common(monkeypatch)
    _seed_pending(message_service, _fallback_pending())

    r = _send(service, "Not now")
    meta = r["assistant_message"].get("metadata") or {}
    assert meta.get("pending_gmail_clarification") in (None, {})
    assert gmail_exec.await_count == 0


def test_negative_cancel_clears_pending_without_search(monkeypatch):
    service, message_service, _ = _make_service()
    gmail_exec = _patch_common(monkeypatch)
    _seed_pending(message_service, _fallback_pending())

    r = _send(service, "Cancel")
    meta = r["assistant_message"].get("metadata") or {}
    assert meta.get("pending_gmail_clarification") in (None, {})
    assert gmail_exec.await_count == 0


def test_ambiguous_response_keeps_pending_without_executing_fallback(monkeypatch):
    service, message_service, _ = _make_service()
    gmail_exec = _patch_common(monkeypatch)
    _seed_pending(message_service, _fallback_pending())

    r = _send(service, "maybe later")
    meta = r["assistant_message"].get("metadata") or {}
    pending = meta.get("pending_gmail_clarification")
    assert isinstance(pending, dict)
    assert pending.get("type") == "gmail_search_fallback_clarification"
    assert gmail_exec.await_count == 0
    assert "reply with" in (r["assistant_message"]["content"] or "").lower()


def test_expired_pending_fallback_cannot_be_consumed(monkeypatch):
    service, message_service, _ = _make_service()
    gmail_exec = _patch_common(monkeypatch)
    expired = _fallback_pending()
    expired["expires_at"] = "2020-01-01T00:00:00+00:00"
    _seed_pending(message_service, expired)

    r = _send(service, "yes")
    meta = r["assistant_message"].get("metadata") or {}
    assert meta.get("pending_gmail_clarification") in (None, {})
    assert gmail_exec.await_count == 0


def test_malformed_pending_fallback_is_cleared_safely(monkeypatch):
    service, message_service, _ = _make_service()
    gmail_exec = _patch_common(monkeypatch)
    malformed = _fallback_pending()
    malformed["expires_at"] = "not-a-date"
    _seed_pending(message_service, malformed)

    r = _send(service, "yes")
    meta = r["assistant_message"].get("metadata") or {}
    assert meta.get("pending_gmail_clarification") in (None, {})
    assert gmail_exec.await_count == 0


def test_pending_fallback_survives_reload_from_conversation_history(monkeypatch):
    # Simulates conversation reload: prior assistant message metadata contains pending fallback.
    service, message_service, _ = _make_service()
    gmail_exec = _patch_common(monkeypatch)
    _seed_pending(message_service, _fallback_pending())
    gmail_exec.return_value = {"messages": [], "search_criteria": "is:unread in:inbox"}

    r = _send(service, "yes")
    meta = r["assistant_message"].get("metadata") or {}
    assert meta.get("pending_gmail_clarification") in (None, {})
    assert gmail_exec.await_count == 1


def test_empty_unread_inbox_result_after_confirmation_does_not_claim_success(monkeypatch):
    service, message_service, _ = _make_service()
    gmail_exec = _patch_common(monkeypatch)
    _seed_pending(message_service, _fallback_pending())
    gmail_exec.return_value = {"messages": [], "search_criteria": "is:unread in:inbox"}

    r = _send(service, "yes")
    text = (r["assistant_message"]["content"] or "").lower()
    meta = r["assistant_message"].get("metadata") or {}
    assert meta.get("pending_gmail_clarification") in (None, {})
    assert gmail_exec.await_count == 1
    # Must be a truthful result state, not a fabricated summary of retrieved emails.
    assert "couldn't find any matching gmail messages" in text or "no " in text or "not" in text


def test_gmail_permission_error_after_confirmation_does_not_claim_success(monkeypatch):
    service, message_service, _ = _make_service()
    gmail_exec = _patch_common(monkeypatch)
    _seed_pending(message_service, _fallback_pending())
    gmail_exec.side_effect = PermissionError("missing scope")

    r = _send(service, "yes")
    text = (r["assistant_message"]["content"] or "").lower()
    meta = r["assistant_message"].get("metadata") or {}
    assert meta.get("pending_gmail_clarification") in (None, {})
    assert gmail_exec.await_count == 1
    assert "permission" in text or "reconnect" in text
    assert "successfully" not in text


def test_gmail_network_failure_after_confirmation_does_not_claim_success(monkeypatch):
    service, message_service, _ = _make_service()
    gmail_exec = _patch_common(monkeypatch)
    _seed_pending(message_service, _fallback_pending())
    gmail_exec.side_effect = Exception("network timeout")

    r = _send(service, "yes")
    text = (r["assistant_message"]["content"] or "").lower()
    meta = r["assistant_message"].get("metadata") or {}
    assert meta.get("pending_gmail_clarification") in (None, {})
    assert gmail_exec.await_count == 1
    assert "could not complete" in text or "try again" in text or "couldn't" in text
    assert "successfully" not in text


def test_repeated_yes_after_resolution_does_not_reexecute_fallback(monkeypatch):
    # First "yes" resolves and clears pending; second "yes" is a new request with no pending state.
    service, message_service, _ = _make_service()
    gmail_exec = _patch_common(monkeypatch)
    _seed_pending(message_service, _fallback_pending())
    gmail_exec.return_value = {"messages": [], "search_criteria": "is:unread in:inbox"}

    r1 = _send(service, "yes")
    assert (r1["assistant_message"].get("metadata") or {}).get("pending_gmail_clarification") in (None, {})
    assert gmail_exec.await_count == 1

    gmail_exec.reset_mock()
    gmail_exec.return_value = {"messages": [], "search_criteria": "is:unread in:inbox"}
    r2 = _send(service, "yes")
    assert r2["user_message"]["content"] == "yes"
    assert (r2["assistant_message"].get("metadata") or {}).get("pending_gmail_clarification") in (None, {})
    assert gmail_exec.await_count == 0


def test_same_idempotency_key_retry_returns_completed_without_reexecuting_graph(monkeypatch):
    service, message_service, request_repository = _make_service()
    gmail_exec = _patch_common(monkeypatch)
    _seed_pending(message_service, _fallback_pending())
    gmail_exec.return_value = {"messages": [], "search_criteria": "is:unread in:inbox"}

    idempotency_key = uuid4()

    # Realistic minimal idempotency behavior for same request: first call creates/completes,
    # second call returns existing completed request and must not re-run execution.
    orig_create = request_repository.create_request
    completed_state = {"record": None}

    def create_request(**kwargs):
        if completed_state["record"] is not None:
            return completed_state["record"], False
        rec, created = orig_create(**kwargs)
        completed_state["record"] = {
            **rec,
            "status": "processing",
            "user_message_id": None,
            "assistant_message_id": None,
            "execution_started_at": None,
            "request_hash": kwargs.get("request_hash"),
        }
        return completed_state["record"], created

    request_repository.create_request = create_request

    orig_get_request = getattr(request_repository, "get_request", None)

    def get_request(**_kwargs):
        rec = completed_state["record"]
        return dict(rec) if rec else None

    request_repository.get_request = get_request

    orig_update_status = request_repository.update_status

    def update_status(*, status, user_message_id=None, assistant_message_id=None, **kwargs):
        rec = completed_state["record"]
        if rec is not None and status == "completed":
            rec = {**rec, "status": "completed", "user_message_id": user_message_id, "assistant_message_id": assistant_message_id}
            completed_state["record"] = rec
        return orig_update_status(status=status, user_message_id=user_message_id, assistant_message_id=assistant_message_id, **kwargs)

    request_repository.update_status = update_status

    # Attach/claim/mark helpers must return record consistent with service expectations.
    request_repository.claim_request = lambda **_kwargs: dict(completed_state["record"])
    request_repository.attach_user_message = lambda **_kwargs: dict(completed_state["record"])
    request_repository.mark_execution_started = lambda **_kwargs: dict(completed_state["record"])

    r1 = service.send_message(
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        content="yes",
        idempotency_key=idempotency_key,
        user_timezone="Asia/Kolkata",
        gmail_connection_id=GMAIL_CONNECTION_ID,
        gmail_enabled=True,
        calendar_enabled=False,
        contacts_enabled=False,
        drive_enabled=False,
    )
    assert gmail_exec.await_count == 1
    assert (r1["assistant_message"].get("metadata") or {}).get("pending_gmail_clarification") in (None, {})

    gmail_exec.reset_mock()
    r2 = service.send_message(
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        content="yes",
        idempotency_key=idempotency_key,
        user_timezone="Asia/Kolkata",
        gmail_connection_id=GMAIL_CONNECTION_ID,
        gmail_enabled=True,
        calendar_enabled=False,
        contacts_enabled=False,
        drive_enabled=False,
    )
    assert gmail_exec.await_count == 0
    assert r2["assistant_message"]["id"] == r1["assistant_message"]["id"]
    assert r2["user_message"]["content"] == "yes"
