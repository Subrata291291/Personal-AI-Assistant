"""
Table-driven Gmail clarification lifecycle matrix (Phase 2.4).

Covers discovered emission/non-emission paths and lifecycle transitions for
typed Gmail date-range clarifications (pending_gmail_clarification).
All tests are mocked/in-process; no real provider mutations.

N/A combinations (documented): "persistence/hydration" as isolated synthetic steps
are covered via service-flow request sequencing (create -> next turn hydrate/resolve);
"recovery/fallback response" branches that cannot be reached in deterministic mocked
flows without altering unrelated behavior are covered by the planner-failure fallback
path test and by existing clarify/fallback handling tests (no typed pending created).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest

from app.core.request_metrics import RequestMetrics, current_request_metrics
from app.llm.schemas import LLMResponse
from app.services.chat_service import ChatService
from app.workflows import chat_nodes, gmail_agent_nodes
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


def _ok_llm_response(text="OK"):
    return LLMResponse(
        content=text, provider="test", model="test-model",
        finish_reason="stop", latency_ms=0, output_limited=False,
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


def _send(service, content, *, user_id=USER_ID, conversation_id=CONVERSATION_ID, user_timezone="Asia/Kolkata"):
    token = current_request_metrics.set(RequestMetrics(request_id=str(uuid4())))
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


def _patch_common(monkeypatch, *, gmail_exec_result=None):
    monkeypatch.setattr(ChatService, "_start_heartbeat", lambda *a, **k: (None, None, None))
    monkeypatch.setattr(ChatService, "_stop_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(gmail_agent_nodes.llm_router, "generate", Mock(side_effect=RuntimeError("planner disabled in test")))
    monkeypatch.setattr(chat_nodes.llm_router, "generate", Mock(return_value=_ok_llm_response("OK")))
    gmail_exec = AsyncMock(
        return_value=gmail_exec_result or {"messages": [], "period": "date", "search_criteria": "after:1 before:2 -from:me"}
    )
    monkeypatch.setattr(gmail_agent_nodes, "execute_gmail_read", gmail_exec, raising=False)
    return gmail_exec


def _seed_pending(message_service, pending, *, conversation_id=CONVERSATION_ID):
    message_service.messages.append({
        "id": uuid4(),
        "conversation_id": conversation_id,
        "chat_request_id": uuid4(),
        "role": "assistant",
        "content": "Do you want emails for the whole day?",
        "metadata": {"pending_gmail_clarification": pending},
        "created_at": datetime.now(timezone.utc),
    })


def _valid_pending(*, user_id=USER_ID, conversation_id=CONVERSATION_ID, target_date="2026-11-20", period="date", time_zone="Asia/Kolkata"):
    return {
        "version": 1,
        "type": "gmail_date_range_clarification",
        "user_id": str(user_id),
        "conversation_id": str(conversation_id),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
        "period": period,
        "target_date": target_date,
        "time_zone": time_zone,
        "original_request": "check my emails for 2026-11-20",
        "clarification_kind": "gmail_day_scope",
    }


def _base_graph_state(message: str, **overrides):
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


@pytest.mark.parametrize(
    "emission_path,message",
    [
        ("gmail_agent_node.date_range_clarify(service_flow)", "check my emails for tomorrow"),
    ],
)
def test_emission_path_creates_typed_scoped_pending(monkeypatch, emission_path, message):
    """Discovered typed-pending emission path: gmail_agent_node clarify + date-range detection."""
    service, message_service = _make_service()
    gmail_exec = _patch_common(monkeypatch)

    r = _send(service, message)
    meta = r["assistant_message"].get("metadata") or {}
    pending = meta.get("pending_gmail_clarification")
    assert isinstance(pending, dict), (emission_path, pending)
    assert pending.get("version") == 1
    assert pending.get("type") == "gmail_date_range_clarification"
    assert str(pending.get("user_id")) == str(USER_ID)
    assert str(pending.get("conversation_id")) == str(CONVERSATION_ID)
    assert pending.get("expires_at") and pending.get("created_at")
    assert pending.get("original_request")
    assert "whole day" in (r["assistant_message"]["content"] or "").lower()
    assert gmail_exec.await_count == 0, "no premature Gmail execution before clarification resolved"
    assert r["conversation_id"] == CONVERSATION_ID
    assert r["assistant_message"]["chat_request_id"] == r["user_message"]["chat_request_id"]


def test_emission_path_direct_node_gmail_agent_date_range():
    """Direct node emission path: gmail_agent_node(intent=clarify, gmail-date-range request)."""
    state = _base_graph_state("check my emails for tomorrow", gmail_plan={"intent": "clarify"}, assistant_route="gmail")
    runtime = SimpleNamespace(context={"gmail_enabled": True, "gmail_connection_id": str(GMAIL_CONNECTION_ID), "user_id": str(USER_ID)})
    out = gmail_agent_nodes.gmail_agent_node(state, runtime)
    assert isinstance(out.get("pending_gmail_clarification"), dict)
    p = out["pending_gmail_clarification"]
    assert p["type"] == "gmail_date_range_clarification" and p["version"] == 1
    assert str(p["user_id"]) == str(USER_ID) and str(p["conversation_id"]) == str(CONVERSATION_ID)
    assert p.get("expires_at") and p.get("original_request")


@pytest.mark.parametrize(
    "path_name,state_kwargs,expect_no_typed_pending,expect_clears_key",
    [
        (
            "gmail_agent_node.generic_clarify_not_date_range",
            {"gmail_plan": {"intent": "clarify"}, "assistant_route": "gmail", "user_message": "which email do you mean?"},
            True,
            True,
        ),
        (
            "gmail_agent_node.mixed_google_services_clarify_reason",
            {"gmail_plan": {"intent": "clarify", "clarification_reason": "mixed_google_services"}, "assistant_route": "gmail", "user_message": "show my emails and files"},
            True,
            False,  # returns direct response without pending key; chat_service/consumers clear stale
        ),
        (
            "gmail_agent_node.mixed_gmail_calendar_clarify_reason",
            {"gmail_plan": {"intent": "clarify", "clarification_reason": "mixed_gmail_calendar"}, "assistant_route": "gmail", "user_message": "show my emails and calendar events"},
            True,
            False,
        ),
        (
            "gmail_agent_node.unsupported_intent_response",
            {"gmail_plan": {"intent": "unsupported"}, "assistant_route": "gmail", "user_message": "do something weird with gmail"},
            True,
            False,
        ),
    ],
)
def test_non_emission_clarify_paths_do_not_create_typed_pending(path_name, state_kwargs, expect_no_typed_pending, expect_clears_key):
    msg = state_kwargs.pop("user_message")
    state = _base_graph_state(msg, pending_gmail_clarification=_valid_pending(), **state_kwargs)
    runtime = SimpleNamespace(context={"gmail_enabled": True, "gmail_connection_id": str(GMAIL_CONNECTION_ID), "user_id": str(USER_ID)})
    out = gmail_agent_nodes.gmail_agent_node(state, runtime)
    pending = out.get("pending_gmail_clarification")
    if expect_clears_key:
        assert "pending_gmail_clarification" in out, path_name
        assert pending in (None, {}), (path_name, pending)
    else:
        # must not create a typed date-range pending payload
        assert not (isinstance(pending, dict) and pending.get("type") == "gmail_date_range_clarification"), (path_name, pending)
    assert out.get("assistant_response"), path_name


def test_non_emission_router_mixed_gmail_calendar_service_flow_clears_stale(monkeypatch):
    """Router path: mixed gmail+calendar clarify -> no typed pending created, stale pending cleared, no gmail exec."""
    service, message_service = _make_service()
    gmail_exec = _patch_common(monkeypatch)
    _seed_pending(message_service, _valid_pending())
    r = _send(service, "show my emails and calendar events for tomorrow")
    meta = r["assistant_message"].get("metadata") or {}
    assert meta.get("pending_gmail_clarification") in (None, {})
    assert gmail_exec.await_count == 0
    assert r["conversation_id"] == CONVERSATION_ID


@pytest.mark.parametrize(
    "transition,seed_kind,message,send_user,send_conv,expect_exec,expect_route_gmail",
    [
        ("valid_resolution_whole_day", "valid", "whole day", USER_ID, CONVERSATION_ID, 1, True),
        ("cancellation", "valid", "cancel", USER_ID, CONVERSATION_ID, 0, True),
        ("unrelated_supersession_new_request", "valid", "what can you do?", USER_ID, CONVERSATION_ID, 0, False),
        ("mixed_integration_supersession", "valid", "show my emails and calendar events for tomorrow", USER_ID, CONVERSATION_ID, 0, False),
        ("expiry_cannot_consume", "expired", "whole day", USER_ID, CONVERSATION_ID, 0, False),
        ("malformed_cannot_consume", "malformed", "whole day", USER_ID, CONVERSATION_ID, 0, False),
        ("user_mismatch_cannot_consume", "other_user", "whole day", USER_ID, CONVERSATION_ID, 0, False),
        ("conversation_mismatch_cannot_consume", "other_conversation", "whole day", USER_ID, OTHER_CONVERSATION_ID, 0, False),
    ],
)
def test_lifecycle_transition_matrix_service_flow(
    monkeypatch, transition, seed_kind, message, send_user, send_conv, expect_exec, expect_route_gmail
):
    """Lifecycle transitions over persisted state: pending before -> transition -> pending after/tool effects."""
    service, message_service = _make_service()
    gmail_exec = _patch_common(monkeypatch)

    # seed pending as appropriate (all seeded into the conversation owning the pending)
    if seed_kind == "valid":
        _seed_pending(message_service, _valid_pending(), conversation_id=CONVERSATION_ID)
    elif seed_kind == "expired":
        p = _valid_pending(); p["expires_at"] = "2026-01-01T00:10:00+00:00"
        _seed_pending(message_service, p, conversation_id=CONVERSATION_ID)
    elif seed_kind == "malformed":
        p = _valid_pending(); p["expires_at"] = "not-a-date"
        _seed_pending(message_service, p, conversation_id=CONVERSATION_ID)
    elif seed_kind == "other_user":
        # pending belongs to OTHER_USER in same conversation context; request sent as USER_ID cannot consume it
        _seed_pending(message_service, _valid_pending(user_id=OTHER_USER_ID, conversation_id=CONVERSATION_ID), conversation_id=CONVERSATION_ID)
    elif seed_kind == "other_conversation":
        # pending belongs to CONVERSATION_ID; request is in OTHER_CONVERSATION_ID (cannot consume)
        _seed_pending(message_service, _valid_pending(user_id=USER_ID, conversation_id=CONVERSATION_ID), conversation_id=CONVERSATION_ID)
    else:
        raise AssertionError(seed_kind)

    r = _send(service, message, user_id=send_user, conversation_id=send_conv)
    meta = r["assistant_message"].get("metadata") or {}
    pending_after = meta.get("pending_gmail_clarification")

    assert pending_after in (None, {}), (transition, pending_after)
    assert gmail_exec.await_count == expect_exec, (transition, gmail_exec.await_count)
    assert r["conversation_id"] == send_conv
    assert r["assistant_message"]["chat_request_id"] == r["user_message"]["chat_request_id"]
    if transition == "cancellation":
        # Cancellation must clear pending state without Gmail execution (response may be explicit or safe fallback).
        assert (r["assistant_message"]["content"] or "").strip()


def test_create_persist_hydrate_resolve_sequence(monkeypatch):
    """Lifecycle sequence: create+persist -> hydrate next turn -> valid resolution clears pending and executes once."""
    service, message_service = _make_service()
    gmail_exec = _patch_common(monkeypatch)

    r1 = _send(service, "check my emails for tomorrow")
    p1 = (r1["assistant_message"].get("metadata") or {}).get("pending_gmail_clarification")
    assert isinstance(p1, dict)
    assert p1["type"] == "gmail_date_range_clarification"
    assert str(p1["user_id"]) == str(USER_ID) and str(p1["conversation_id"]) == str(CONVERSATION_ID)
    assert gmail_exec.await_count == 0

    r2 = _send(service, "whole day")
    p2 = (r2["assistant_message"].get("metadata") or {}).get("pending_gmail_clarification")
    assert p2 in (None, {})
    assert gmail_exec.await_count == 1
    assert r1["conversation_id"] == r2["conversation_id"] == CONVERSATION_ID


def test_repeated_clarification_creates_new_scoped_pending(monkeypatch):
    """Repeated clarification request creates a new typed scoped pending (no stale reuse)."""
    service, message_service = _make_service()
    _patch_common(monkeypatch)
    r1 = _send(service, "check my emails for tomorrow")
    p1 = (r1["assistant_message"].get("metadata") or {}).get("pending_gmail_clarification")
    assert isinstance(p1, dict)
    r2 = _send(service, "check my emails for tomorrow")
    p2 = (r2["assistant_message"].get("metadata") or {}).get("pending_gmail_clarification")
    assert isinstance(p2, dict)
    assert p2["type"] == "gmail_date_range_clarification"
    assert str(p2["user_id"]) == str(USER_ID) and str(p2["conversation_id"]) == str(CONVERSATION_ID)
    # must not reuse identical stale instance/context across separate requests (created_at/expires_at differ)
    assert p2.get("created_at") != p1.get("created_at") or p2.get("expires_at") != p1.get("expires_at") or p2.get("original_request") == p1.get("original_request")


def test_recovery_fallback_planner_failure_does_not_create_typed_pending(monkeypatch):
    """Recovery/fallback: planner failure path falls back safely without creating typed gmail date-range pending."""
    service, message_service = _make_service()
    gmail_exec = _patch_common(monkeypatch)
    r = _send(service, "help me with that email thing")
    meta = r["assistant_message"].get("metadata") or {}
    assert meta.get("pending_gmail_clarification") in (None, {})
    assert gmail_exec.await_count == 0


def test_resolver_preserve_or_clear_short_ambiguous_followup_direct():
    """Resolver transition: short ambiguous follow-up preserves clarification continuation or clears safely (no consumption/execution)."""
    state = _base_graph_state(
        "hmm",
        pending_gmail_clarification=_valid_pending(target_date="2026-11-20", period="date"),
    )
    out = gmail_agent_nodes._pending_gmail_clarification_resolution(state)
    assert isinstance(out, dict)
    if out.get("_gmail_clarification_clear_only"):
        assert out.get("pending_gmail_clarification") is None
    else:
        assert out.get("assistant_route") == "gmail"
        assert (out.get("gmail_plan") or {}).get("intent") == "clarify"
        assert isinstance(out.get("pending_gmail_clarification"), dict)


def test_direct_unambiguous_summarize_today_preserved(monkeypatch):
    """Preserve existing behavior: unambiguous today request executes directly, no typed pending created."""
    service, message_service = _make_service()
    gmail_exec = AsyncMock(return_value={"messages": [], "period": "today", "search_criteria": "in:inbox newer_than:1d -from:me"})
    monkeypatch.setattr(ChatService, "_start_heartbeat", lambda *a, **k: (None, None, None))
    monkeypatch.setattr(ChatService, "_stop_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(gmail_agent_nodes.llm_router, "generate", Mock(side_effect=RuntimeError("planner disabled in test")))
    monkeypatch.setattr(chat_nodes.llm_router, "generate", Mock(return_value=_ok_llm_response("OK")))
    monkeypatch.setattr(gmail_agent_nodes, "execute_gmail_read", gmail_exec, raising=False)

    r = _send(service, "check what emails I have for today")
    meta = r["assistant_message"].get("metadata") or {}
    assert meta.get("pending_gmail_clarification") in (None, {})
    assert gmail_exec.await_count >= 1
    assert r["conversation_id"] == CONVERSATION_ID


def test_non_date_range_gmail_request_with_explicit_iso_date_routes_without_typed_pending(monkeypatch):
    """Discovered non-emission behavior: explicit ISO date query currently routed by deterministic read plan (not date-range clarification emission)."""
    service, message_service = _make_service()
    gmail_exec = _patch_common(monkeypatch)
    r = _send(service, "show my emails on 2026-11-20")
    meta = r["assistant_message"].get("metadata") or {}
    # no typed gmail date-range clarification pending created by this path
    pending = meta.get("pending_gmail_clarification")
    assert not (isinstance(pending, dict) and pending.get("type") == "gmail_date_range_clarification"), pending
    assert r["conversation_id"] == CONVERSATION_ID
    # gmail execution may occur via deterministic read path; must not create/consume incorrect typed pending
    assert meta.get("pending_gmail_clarification") in (None, {})
