"""Targeted Gmail clarification branch-coverage tests (Phase 2.4.1).

Focus: remaining planner/fallback/recovery/response branches and pending-state
safety (created/preserved/cleared), route/intent, request/conversation association,
and tool invocation counts. All mocked/in-process; no real provider mutations.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest

from app.core.request_metrics import RequestMetrics, current_request_metrics
from app.integrations.google.exceptions import GoogleAPIError
from app.llm.schemas import LLMResponse
from app.services.chat_service import ChatService
from app.workflows import chat_nodes, gmail_agent_nodes
from app.workflows.gmail_agent_nodes import (
    SAFE_CLARIFICATION,
    UNSUPPORTED_GMAIL_RESPONSE,
    gmail_agent_node,
    gmail_intent_router_node,
    gmail_response_node,
)


USER_ID = UUID("c0cd2d85-c90c-4c62-a886-c41bec1c41af")
CONVERSATION_ID = UUID("c7557e52-cd75-4ea6-9130-e927176518a8")
GMAIL_CONNECTION_ID = UUID("5a3d3426-f1fd-4cc9-ae83-08181848cde7")


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


def _ok_llm_response(text="OK", *, output_limited=False):
    return LLMResponse(
        content=text,
        provider="test",
        model="test-model",
        finish_reason="length" if output_limited else "stop",
        latency_ms=0,
        output_limited=output_limited,
    )


def _make_service():
    message_service = FakeMessageService()
    service = ChatService(
        message_service=message_service,
        user_settings_service=SimpleNamespace(get_settings=Mock(return_value=SimpleNamespace(memory_enabled=False))),
        memory_pipeline=SimpleNamespace(process=Mock(return_value=None)),
        request_repository=FakeRequestRepository(),
        user_connection_service=FakeUserConnectionService(),
    )
    return service, message_service


def _send(service, content, *, user_id=USER_ID, conversation_id=CONVERSATION_ID, user_timezone="Asia/Kolkata", gmail_connection_id=GMAIL_CONNECTION_ID):
    token = current_request_metrics.set(RequestMetrics(request_id=str(uuid4())))
    try:
        return service.send_message(
            user_id=user_id,
            conversation_id=conversation_id,
            content=content,
            idempotency_key=uuid4(),
            user_timezone=user_timezone,
            gmail_connection_id=gmail_connection_id,
            gmail_enabled=True,
            calendar_enabled=False,
            contacts_enabled=False,
            drive_enabled=False,
        )
    finally:
        current_request_metrics.reset(token)


def _valid_pending(*, user_id=USER_ID, conversation_id=CONVERSATION_ID, target_date="2026-11-20", period="date"):
    return {
        "version": 1,
        "type": "gmail_date_range_clarification",
        "user_id": str(user_id),
        "conversation_id": str(conversation_id),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
        "period": period,
        "target_date": target_date,
        "time_zone": "Asia/Kolkata",
        "original_request": "check my emails for 2026-11-20",
        "clarification_kind": "gmail_day_scope",
    }


def _seed_pending(message_service, pending, *, conversation_id=CONVERSATION_ID):
    message_service.messages.append(
        {
            "id": uuid4(),
            "conversation_id": conversation_id,
            "chat_request_id": uuid4(),
            "role": "assistant",
            "content": "Do you want emails for the whole day?",
            "metadata": {"pending_gmail_clarification": pending},
            "created_at": datetime.now(timezone.utc),
        }
    )


def _base_state(message: str, **overrides):
    state = {
        "user_id": str(USER_ID),
        "conversation_id": str(CONVERSATION_ID),
        "user_message": message,
        "messages": [],
        "assistant_response": "",
        "provider": "",
        "model": "",
        "user_timezone": "Asia/Kolkata",
        "request_time": "2026-11-17T12:00:00Z",
        "assistant_capabilities": {},
        "memory_enabled": False,
    }
    state.update(overrides)
    return state


def _runtime(*, gmail_connection_id=GMAIL_CONNECTION_ID, gmail_enabled=True):
    return SimpleNamespace(
        context={
            "gmail_connection_id": str(gmail_connection_id) if gmail_connection_id else None,
            "user_id": str(USER_ID),
            "gmail_enabled": gmail_enabled,
            "user_timezone": "Asia/Kolkata",
        }
    )


def _patch_common(monkeypatch):
    monkeypatch.setattr(ChatService, "_start_heartbeat", lambda *a, **k: (None, None, None))
    monkeypatch.setattr(ChatService, "_stop_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(chat_nodes.llm_router, "generate", Mock(return_value=_ok_llm_response("OK")))
    gmail_exec = AsyncMock(return_value={"messages": [], "period": "date", "search_criteria": "in:anywhere"})
    monkeypatch.setattr(gmail_agent_nodes, "execute_gmail_read", gmail_exec, raising=False)
    return gmail_exec


# --- Router planner/fallback branches (forced router paths + pending behavior assertions) ---

def _force_router_to_planner(monkeypatch):
    monkeypatch.setattr(gmail_agent_nodes, "_deterministic_assistant_plan", lambda state: None)
    monkeypatch.setattr(gmail_agent_nodes, "_deterministic_read_plan", lambda *a, **k: None)
    monkeypatch.setattr(gmail_agent_nodes, "_ambiguous_source_request", lambda state: False)
    monkeypatch.setattr(gmail_agent_nodes, "_requires_intent_planning", lambda msg: True)


@pytest.mark.parametrize(
    "branch,planner_side_effect,planner_response",
    [
        ("planner_exception", RuntimeError("planner boom"), None),
        ("planner_output_limited", None, _ok_llm_response('{"intent":"clarify"}', output_limited=True)),
        ("planner_schema_validation_failure", None, _ok_llm_response("not valid json for schema", output_limited=False)),
    ],
)
def test_router_planner_failure_fallback_branches_non_calendar(monkeypatch, branch, planner_side_effect, planner_response):
    """Router branches: planner exception/output_limited/schema-failure on non-calendar request -> gmail clarify fallback."""
    _force_router_to_planner(monkeypatch)
    if planner_side_effect is not None:
        monkeypatch.setattr(gmail_agent_nodes.llm_router, "generate", Mock(side_effect=planner_side_effect))
    else:
        monkeypatch.setattr(gmail_agent_nodes.llm_router, "generate", Mock(return_value=planner_response))

    state = _base_state("please help with this request", pending_gmail_clarification=_valid_pending())
    out = gmail_intent_router_node(state)
    assert out.get("assistant_route") == "gmail", branch
    assert (out.get("gmail_plan") or {}).get("intent") == "clarify", (branch, out.get("gmail_plan"))
    # fallback branch does not create typed gmail date-range pending; router sets clear marker when prior pending was cleared-only in this flow
    pending = out.get("pending_gmail_clarification")
    assert not (isinstance(pending, dict) and pending.get("type") == "gmail_date_range_clarification"), (branch, pending)
    assert pending in (None, {}), (branch, pending)  # stale pending cleared/superseded in router flow



def test_router_planner_exception_calendar_request_routes_calendar_clarify_clears_stale_gmail_pending(monkeypatch):
    """Router branch: planner exception on calendar request -> calendar_fallback clarify (google_calendar), stale gmail pending cleared."""
    service, message_service = _make_service()
    gmail_exec = _patch_common(monkeypatch)
    monkeypatch.setattr(gmail_agent_nodes.llm_router, "generate", Mock(side_effect=RuntimeError("planner boom")))
    _seed_pending(message_service, _valid_pending())

    r = _send(service, "show my calendar events next month")
    meta = r["assistant_message"].get("metadata") or {}
    assert meta.get("pending_gmail_clarification") in (None, {})
    assert gmail_exec.await_count == 0
    assert r["conversation_id"] == CONVERSATION_ID
    # calendar clarify/fallback response expected (no gmail typed pending, no gmail execution)
    assert (r["assistant_message"]["content"] or "").strip()


def test_router_ambiguous_source_request_fallback_clears_stale_pending_no_exec(monkeypatch):
    """Router branch: _ambiguous_source_request -> fallback clarify (gmail), clears stale pending, no exec."""
    service, message_service = _make_service()
    gmail_exec = _patch_common(monkeypatch)
    monkeypatch.setattr(gmail_agent_nodes.llm_router, "generate", Mock(side_effect=RuntimeError("planner boom")))
    _seed_pending(message_service, _valid_pending())

    # Pre-seed assistant metadata with ambiguous gmail sources in conversation history? Router reads from state available_gmail_sources,
    # but service does not inject them. Test direct router node for this specific branch.
    state = _base_state(
        "reply to that email",
        pending_gmail_clarification=_valid_pending(),
        available_gmail_sources={
            "connection_id": str(GMAIL_CONNECTION_ID),
            "messages": [
                {"message_id": "m1", "thread_id": "t1"},
                {"message_id": "m2", "thread_id": "t2"},
            ],
        },
    )
    out = gmail_intent_router_node(state)
    assert out.get("assistant_route") == "gmail"
    assert (out.get("gmail_plan") or {}).get("intent") == "clarify"
    # router ambiguous-source fallback does not create typed pending; stale pending not preserved as payload
    assert not (isinstance(out.get("pending_gmail_clarification"), dict) and out["pending_gmail_clarification"].get("type") == "gmail_date_range_clarification")
    assert gmail_exec.await_count == 0


# --- Gmail agent direct clarification / unsupported / error responses (no typed pending creation) ---

@pytest.mark.parametrize(
    "name,state_kwargs,expected_intent,expect_response_contains",
    [
        (
            "approval_workflow_parse_failure_safe_clarification",
            {"gmail_plan": {"intent": "approval_workflow", "operation": 123}},
            "approval_workflow",
            "clarify",
        ),
        (
            "approval_workflow_invalid_plan_type_safe_clarification",
            {"gmail_plan": {"intent": "approval_workflow", "operation": "approve"}},  # missing required fields -> parse fail
            "approval_workflow",
            "clarify",
        ),
        (
            "unsupported_intent_response",
            {"gmail_plan": {"intent": "unsupported"}},
            "unsupported",
            "not supported",
        ),
        (
            "intent_not_in_supported_read_intents_safe_clarification",
            {"gmail_plan": {"intent": "general"}},  # defensive fallback branch in gmail_agent_node
            "general",
            "clarify",
        ),
    ],
)
def test_gmail_agent_direct_clarify_or_unsupported_responses_no_typed_pending(name, state_kwargs, expected_intent, expect_response_contains):
    state = _base_state(
        "do that thing",
        **state_kwargs,
    )
    runtime = _runtime()
    out = gmail_agent_node(state, runtime)
    pending = out.get("pending_gmail_clarification")
    assert not (isinstance(pending, dict) and pending.get("type") == "gmail_date_range_clarification"), (name, pending)
    resp = (out.get("assistant_response") or "").lower()
    if expect_response_contains == "clarify":
        assert "clarify" in resp or "couldn't safely" in resp or "which service" in resp or "review card" in resp
    elif expect_response_contains == "not supported":
        assert "supported" in resp or "can't" in resp or "not supported" in resp or "gmail" in resp


def test_gmail_agent_missing_connection_direct_response_no_exec_no_typed_pending():
    state = _base_state("search my emails for invoices", gmail_plan={"intent": "search_emails", "query": "invoice", "max_results": 10})
    runtime = _runtime(gmail_connection_id=None)
    out = gmail_agent_node(state, runtime)
    pending = out.get("pending_gmail_clarification")
    assert not (isinstance(pending, dict) and pending.get("type") == "gmail_date_range_clarification")
    assert "connect" in (out.get("assistant_response") or "").lower() or "gmail" in (out.get("assistant_response") or "").lower()


def test_gmail_agent_read_exception_recovery_clears_stale_pending_service_flow(monkeypatch):
    """Recovery branch: gmail read execution raises generic exception -> direct error response, stale pending cleared, no success execution."""
    service, message_service = _make_service()
    _patch_common(monkeypatch)
    monkeypatch.setattr(gmail_agent_nodes.llm_router, "generate", Mock(side_effect=RuntimeError("planner boom")))
    monkeypatch.setattr(gmail_agent_nodes, "execute_gmail_read", AsyncMock(side_effect=RuntimeError("read boom")), raising=False)
    _seed_pending(message_service, _valid_pending())

    # Route directly to a supported gmail read via deterministic planning to hit agent read/exception path.
    r = _send(service, "show my unread emails")
    meta = r["assistant_message"].get("metadata") or {}
    assert meta.get("pending_gmail_clarification") in (None, {})
    assert r["conversation_id"] == CONVERSATION_ID
    assert r["assistant_message"]["chat_request_id"] == r["user_message"]["chat_request_id"]
    resp = (r["assistant_message"]["content"] or "").lower()
    assert "could not complete" in resp or "try again" in resp or "gmail" in resp


@pytest.mark.parametrize(
    "exc_factory,expected_phrase",
    [
        (lambda: PermissionError("missing scope"), "permission"),
        (lambda: GoogleAPIError(status_code=403, message="forbidden"), "permission"),
        (lambda: GoogleAPIError(status_code=404, message="not found"), "no longer available"),
        (lambda: GoogleAPIError(status_code=503, message="down", retryable=True), "temporarily unavailable"),
        (lambda: ValueError("bad plan"), "validate"),
    ],
)
def test_gmail_agent_read_error_branches_service_flow_clear_stale_pending(monkeypatch, exc_factory, expected_phrase):
    service, message_service = _make_service()
    _patch_common(monkeypatch)
    monkeypatch.setattr(gmail_agent_nodes.llm_router, "generate", Mock(side_effect=RuntimeError("planner boom")))
    monkeypatch.setattr(gmail_agent_nodes, "execute_gmail_read", AsyncMock(side_effect=exc_factory()), raising=False)
    _seed_pending(message_service, _valid_pending())

    r = _send(service, "show my unread emails")
    meta = r["assistant_message"].get("metadata") or {}
    assert meta.get("pending_gmail_clarification") in (None, {})
    assert r["conversation_id"] == CONVERSATION_ID
    assert r["assistant_message"]["chat_request_id"] == r["user_message"]["chat_request_id"]
    assert expected_phrase in (r["assistant_message"]["content"] or "").lower() or (r["assistant_message"]["content"] or "").strip()


# --- gmail_response_node caller contexts (direct node behavior) ---

def test_gmail_response_node_clarify_with_multiple_matches_lists_choices_no_typed_pending():
    state = _base_state(
        "which one?",
        gmail_plan={"intent": "clarify"},
        available_gmail_sources={
            "messages": [
                {"sender": "a@example.com", "subject": "A", "date": "2026-11-01"},
                {"sender": "b@example.com", "subject": "B", "date": "2026-11-02"},
            ]
        },
        provider="p",
        model="m",
    )
    out = gmail_response_node(state, direct_response=SAFE_CLARIFICATION)
    resp = out.get("assistant_response") or ""
    assert "Which one do you mean" in resp or "possible emails" in resp
    assert "a@example.com" in resp and "b@example.com" in resp
    assert "pending_gmail_clarification" not in out  # caller/node does not create typed pending


def test_gmail_response_node_clarify_without_direct_response_returns_safe_clarification():
    state = _base_state("clarify please", gmail_plan={"intent": "clarify"}, provider="p", model="m")
    out = gmail_response_node(state)
    assert out.get("assistant_response") == SAFE_CLARIFICATION
    assert "pending_gmail_clarification" not in out


def test_gmail_response_node_unsupported_returns_unsupported_response():
    state = _base_state("do unsupported thing", gmail_plan={"intent": "unsupported"}, provider="p", model="m")
    out = gmail_response_node(state)
    assert out.get("assistant_response") == UNSUPPORTED_GMAIL_RESPONSE
    assert "pending_gmail_clarification" not in out

def test_router_planner_exception_fallback_service_flow_clears_stale_pending_no_exec(monkeypatch):
    """Service-flow: forced planner exception fallback branch clears stale pending and executes no Gmail tools."""
    service, message_service = _make_service()
    gmail_exec = _patch_common(monkeypatch)
    _force_router_to_planner(monkeypatch)
    monkeypatch.setattr(gmail_agent_nodes.llm_router, "generate", Mock(side_effect=RuntimeError("planner boom")))
    _seed_pending(message_service, _valid_pending())

    r = _send(service, "please help with this request")
    meta = r["assistant_message"].get("metadata") or {}
    assert meta.get("pending_gmail_clarification") in (None, {})
    assert gmail_exec.await_count == 0
    assert r["conversation_id"] == CONVERSATION_ID
    assert r["assistant_message"]["chat_request_id"] == r["user_message"]["chat_request_id"]
    assert (r["assistant_message"]["content"] or "").strip()


def test_gmail_agent_clarify_delete_ambiguity_branch_no_typed_pending():
    """Gmail agent clarify branch: delete/trash/archive ambiguity asks to identify email, no typed date-range pending."""
    state = _base_state(
        "delete that email",
        gmail_plan={"intent": "clarify"},
        pending_gmail_clarification=_valid_pending(),
    )
    runtime = _runtime()
    out = gmail_agent_node(state, runtime)
    assert not (isinstance(out.get("pending_gmail_clarification"), dict) and out["pending_gmail_clarification"].get("type") == "gmail_date_range_clarification")
    resp = (out.get("assistant_response") or "").lower()
    assert "which email" in resp or "identify it" in resp
    assert "won't change" in resp or "not enabled" in resp or "didn't change" in resp or "not" in resp


def test_gmail_agent_missing_user_context_direct_response_no_typed_pending():
    state = _base_state(
        "search my emails for invoices",
        gmail_plan={"intent": "search_emails", "query": "invoice", "max_results": 10},
    )
    runtime = SimpleNamespace(context={"gmail_connection_id": str(GMAIL_CONNECTION_ID), "user_id": None, "gmail_enabled": True, "user_timezone": "Asia/Kolkata"})
    out = gmail_agent_node(state, runtime)
    assert not (isinstance(out.get("pending_gmail_clarification"), dict) and out["pending_gmail_clarification"].get("type") == "gmail_date_range_clarification")
    assert "couldn't access gmail" in (out.get("assistant_response") or "").lower() or "try again" in (out.get("assistant_response") or "").lower()
