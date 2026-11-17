import asyncio
import json
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest

from app.core.request_metrics import RequestMetrics, current_request_metrics
from app.llm.fallback import FallbackManager
from app.llm.schemas import LLMRequest, LLMResponse
from app.services import chat_service as chat_service_module
from app.services.chat_service import ChatService
from app.workflows import gmail_agent_nodes


USER_ID = UUID("11111111-2222-3333-4444-555555555555")
CONVERSATION_ID = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
CONTACTS_CONNECTION_ID = UUID("99999999-8888-7777-6666-555555555555")


class _FakeRequestRecord:
    def __init__(self):
        self.id = uuid4()
        self.status = "processing"
        self.user_message_id = None
        self.assistant_message_id = None
        self.worker_id = "test-worker"
        self.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        self.execution_started_at = None
        self.error_message = None

    def to_dict(self):
        return {
            "id": self.id,
            "status": self.status,
            "user_message_id": self.user_message_id,
            "assistant_message_id": self.assistant_message_id,
            "worker_id": self.worker_id,
            "lease_expires_at": self.lease_expires_at,
            "execution_started_at": self.execution_started_at,
            "error_message": self.error_message,
        }


class FakeRequestRepository:
    def __init__(self):
        self.records = {}

    def _key(self, user_id, conversation_id, idempotency_key):
        return (str(user_id), str(conversation_id), str(idempotency_key))

    def create_request(self, *, user_id, conversation_id, idempotency_key, request_hash):
        key = self._key(user_id, conversation_id, idempotency_key)
        rec = _FakeRequestRecord()
        self.records[key] = rec
        return rec.to_dict(), True

    def get_request(self, *, user_id, conversation_id, idempotency_key):
        key = self._key(user_id, conversation_id, idempotency_key)
        rec = self.records.get(key)
        return rec.to_dict() if rec else None

    def claim_request(self, *, user_id, conversation_id, idempotency_key, worker_id, lease_seconds=600):
        rec = self.records.get(self._key(user_id, conversation_id, idempotency_key))
        if not rec:
            return None
        rec.worker_id = worker_id
        rec.lease_expires_at = datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)
        return rec.to_dict()

    def attach_user_message(self, *, user_id, conversation_id, idempotency_key, worker_id, user_message_id):
        rec = self.records.get(self._key(user_id, conversation_id, idempotency_key))
        if not rec:
            return None
        rec.user_message_id = user_message_id
        return rec.to_dict()

    def mark_execution_started(self, *, user_id, conversation_id, idempotency_key, worker_id):
        rec = self.records.get(self._key(user_id, conversation_id, idempotency_key))
        if not rec:
            return None
        rec.execution_started_at = datetime.now(timezone.utc)
        return rec.to_dict()

    def update_status(self, *, user_id, conversation_id, idempotency_key, worker_id, status, user_message_id=None, assistant_message_id=None, error_message=None):
        rec = self.records.get(self._key(user_id, conversation_id, idempotency_key))
        if not rec:
            return None
        rec.status = status
        if user_message_id is not None:
            rec.user_message_id = user_message_id
        if assistant_message_id is not None:
            rec.assistant_message_id = assistant_message_id
        rec.error_message = error_message
        rec.worker_id = None
        rec.lease_expires_at = None
        return rec.to_dict()

    def reconcile_completed_request(self, *, user_id, conversation_id, idempotency_key, user_message_id, assistant_message_id):
        rec = self.records.get(self._key(user_id, conversation_id, idempotency_key))
        if not rec:
            return None
        rec.status = "completed"
        rec.user_message_id = user_message_id
        rec.assistant_message_id = assistant_message_id
        rec.error_message = None
        rec.worker_id = None
        rec.lease_expires_at = None
        return rec.to_dict()

    def renew_lease(self, **_kwargs):
        return {"renewed": True}


class FakeMessageService:
    def __init__(self):
        self.messages = []

    def verify_conversation_ownership(self, **_kwargs):
        return True

    def get_request_message(self, *, user_id, conversation_id, chat_request_id, role):
        for message in reversed(self.messages):
            if str(message.get("conversation_id")) != str(conversation_id):
                continue
            if str(message.get("chat_request_id")) != str(chat_request_id):
                continue
            if message.get("role") == role:
                return message
        return None

    def create_request_message(self, *, conversation_id, chat_request_id, role, content, metadata=None):
        message = {
            "id": uuid4(),
            "conversation_id": str(conversation_id),
            "chat_request_id": str(chat_request_id),
            "role": role,
            "content": content,
            "metadata": metadata or {},
            "created_at": datetime.now(timezone.utc),
        }
        self.messages.append(message)
        return message

    def get_message(self, *, user_id, conversation_id, message_id):
        for message in self.messages:
            if str(message.get("conversation_id")) != str(conversation_id):
                continue
            if str(message.get("id")) == str(message_id):
                return message
        raise LookupError("Message not found")

    def get_conversation_messages(self, *, user_id, conversation_id, limit=100):
        msgs = [m for m in self.messages if str(m.get("conversation_id")) == str(conversation_id)]
        return msgs[-limit:]

    def update_request_message_content(self, **_kwargs):
        return None


class FakeUserConnectionService:
    db = object()

    def get_granted_google_scopes(self, **_kwargs):
        return {"https://www.googleapis.com/auth/contacts.readonly"}

    def get_default_google_connection_for_scopes(self, **_kwargs):
        return None

    def get_user_calendar_access(self, **_kwargs):
        return {
            "google_connection_available": True,
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
            "google_connection_available": True,
            "capabilities": {"contacts_read": True},
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


class _FakeProvider:
    provider_name = "fake"
    model = "fake-model"

    def __init__(self, name, model, behavior):
        self.provider_name = name
        self.model = model
        self.behavior = behavior
        self.calls = 0

    def generate(self, request):
        self.calls += 1
        result = self.behavior(request)
        if isinstance(result, Exception):
            raise result
        return result

    def stream(self, request):
        self.calls += 1
        result = self.behavior(request)
        if isinstance(result, Exception):
            raise result
        return iter(result)


class _FakeRegistry:
    def __init__(self, configured, providers):
        self._configured = set(configured)
        self._providers = providers

    def is_configured(self, provider_name: str) -> bool:
        return provider_name in self._configured

    def get_provider(self, provider_name: str):
        provider = self._providers.get(provider_name)
        if provider is None:
            raise RuntimeError(f"Provider {provider_name} not configured.")
        return provider


def _ok(provider, model="test-model", content="ok"):
    return LLMResponse(content=content, provider=provider, model=model, latency_ms=1.0)


def _make_contacts_tools(monkeypatch):
    """Mock contacts_read tool execution so no real Google APIs are called."""
    from app.workflows import contacts_read

    tool_calls = {"search": 0, "list": 0, "get": 0}

    class _Tool:
        def __init__(self, name, fn):
            self.name = name
            self.fn = fn
        async def ainvoke(self, args):
            return self.fn(args)

    def search_contacts(args):
        tool_calls["search"] += 1
        # Return two matching contacts for "subbhro"
        return json.dumps({
            "ok": True,
            "data": {
                "contacts": [
                    {
                        "resource_name": "people/1",
                        "display_name": "Subbhro Das",
                        "primary_phone": "111-111",
                        "phones": ["111-111"],
                    },
                    {
                        "resource_name": "people/2",
                        "display_name": "Subbhro Roy",
                        "primary_phone": "222-222",
                        "phones": ["222-222"],
                    },
                ],
                "next_page_token": None,
            },
            "error": None,
        })

    def list_contacts(args):
        tool_calls["list"] += 1
        return json.dumps({"ok": True, "data": {"contacts": [], "next_page_token": None}, "error": None})

    def get_contact(args):
        tool_calls["get"] += 1
        rn = args.get("resource_name")
        if rn == "people/1":
            return json.dumps({
                "ok": True,
                "data": {
                    "resource_name": "people/1",
                    "display_name": "Subbhro Das",
                    "primary_phone": "111-111",
                    "phones": ["111-111"],
                },
                "error": None,
            })
        if rn == "people/2":
            return json.dumps({
                "ok": True,
                "data": {
                    "resource_name": "people/2",
                    "display_name": "Subbhro Roy",
                    "primary_phone": "222-222",
                    "phones": ["222-222"],
                },
                "error": None,
            })
        return json.dumps({"ok": False, "data": None, "error": {"message": "not found"}})

    tools = {
        "search_contacts": _Tool("search_contacts", search_contacts),
        "list_contacts": _Tool("list_contacts", list_contacts),
        "get_contact": _Tool("get_contact", get_contact),
    }

    monkeypatch.setattr(contacts_read, "create_contacts_tools", lambda **_kwargs: list(tools.values()))
    return tool_calls, contacts_read


def _make_service():
    message_service = FakeMessageService()
    request_repository = FakeRequestRepository()
    service = ChatService(
        message_service=message_service,
        user_settings_service=SimpleNamespace(
            get_settings=Mock(return_value=SimpleNamespace(memory_enabled=False))
        ),
        memory_pipeline=SimpleNamespace(process=Mock(return_value=None)),
        request_repository=request_repository,
        user_connection_service=FakeUserConnectionService(),
    )
    return service, message_service, request_repository


@pytest.mark.parametrize("use_fallback_chain", [True])
def test_contacts_clarification_selection_preserved_through_chatservice_langgraph_with_llm_fallback(
    monkeypatch, use_fallback_chain
):
    """Priority 1: ChatService + LangGraph path with mocked provider fallback.

    Turn 1: "show me subbhro number" -> assistant asks clarification (2 contacts).
    Turn 2: "1" -> same user/conversation/request identity context; LLM providers
    experience Groq timeout, OpenRouter timeout, Gemini success for LLM calls in
    that request; assistant returns first selected contact's number.

    Google APIs/tools are mocked (no real calls). Provider adapters are mocked via
    fake providers/registry with controlled timeout exceptions.
    """
    from app.llm import router as llm_router_module

    tool_calls, contacts_read = _make_contacts_tools(monkeypatch)

    # Keep turn 1 routing deterministic for stability. Turn 2 is forced to LLM below
    # so real fallback (Groq timeout -> OpenRouter timeout -> Gemini success) executes
    # in the same logical clarification-follow-up request while contacts selection
    # remains trusted/deterministic in the contacts agent.
    def _deterministic_plan_turn1(state):
        msg = (state.get("user_message") or "").casefold()
        if "subbhro" in msg and "number" in msg:
            return {"intent": "google_contacts"}, "google_contacts"
        return None
    monkeypatch.setattr(gmail_agent_nodes, "_deterministic_assistant_plan", _deterministic_plan_turn1)
    monkeypatch.setattr(gmail_agent_nodes, "_pending_calendar_continuation", lambda _s: None, raising=False)
    monkeypatch.setattr(gmail_agent_nodes, "_pending_gmail_clarification_resolution", lambda _s: None, raising=False)
    monkeypatch.setattr(gmail_agent_nodes, "_calendar_resume_disabled_integration", lambda _s: None, raising=False)
    # Ensure contacts intent extraction is deterministic for the initial query in this
    # controlled integration test (tool results still come from mocked People API tools).
    def _deterministic_extract(message: str):
        text = (message or "").casefold()
        if "subbhro" in text and "number" in text:
            return contacts_read.ContactsReadIntent(
                "get_contact_phone", "phone", "Subbhro", source="deterministic"
            )
        if text.strip() == "1":
            return contacts_read.ContactsReadIntent("unknown", "none", None, source="deterministic")
        return contacts_read.extract_contacts_read_intent(message)
    monkeypatch.setattr(contacts_read, "resolve_contacts_read_intent", lambda m: (_deterministic_extract(m), None, "deterministic"))

    # Fake provider chain for fallback manager: Groq timeout, OpenRouter timeout, Gemini success.
    fallback_attempts = {"groq": 0, "openrouter": 0, "gemini": 0}

    def groq_timeout(_req):
        fallback_attempts["groq"] += 1
        raise TimeoutError("groq timeout")

    def openrouter_timeout(_req):
        fallback_attempts["openrouter"] += 1
        raise TimeoutError("openrouter timeout")

    def gemini_ok(_req):
        fallback_attempts["gemini"] += 1
        # Minimal schema-valid intent for router planning in this integration scenario.
        return _ok(
            "gemini",
            "gemini-model",
            content=json.dumps({"intent": "google_contacts"}),
        )

    providers = {
        "groq": _FakeProvider("groq", "groq-model", groq_timeout),
        "openrouter": _FakeProvider("openrouter", "openrouter-model", openrouter_timeout),
        "gemini": _FakeProvider("gemini", "gemini-model", gemini_ok),
    }
    fake_registry = _FakeRegistry(["groq", "openrouter", "gemini"], providers)
    fake_fallback = FallbackManager(registry=fake_registry)
    # Force priority for deterministic test
    fake_fallback.provider_priority = ["groq", "openrouter", "gemini"]

    # Patch the active router's fallback manager so any LLM call in this request uses
    # real fallback logic with mocked provider adapters/timeouts.
    monkeypatch.setattr(llm_router_module.llm_router, "fallback_manager", fake_fallback)
    # Also patch the router imported by workflow modules if it references a different object.
    monkeypatch.setattr(gmail_agent_nodes, "llm_router", llm_router_module.llm_router, raising=False)
    monkeypatch.setattr(contacts_read, "llm_router", llm_router_module.llm_router, raising=False)

    # Disable heartbeat threads and keep request metrics context.
    monkeypatch.setattr(ChatService, "_start_heartbeat", lambda _self, *_a, **_k: (None, None, None))
    monkeypatch.setattr(ChatService, "_stop_heartbeat", lambda _self, **_k: None)
    # Avoid DB-backed approval hydration side effects
    monkeypatch.setattr(ChatService, "_hydrate_action_approval_metadata", lambda self, history, **_k: history)

    service, message_service, request_repository = _make_service()

    token = current_request_metrics.set(RequestMetrics(request_id="itest-contacts-fallback"))
    try:
        # Turn 1: clarification
        resp1 = service.send_message(
            user_id=USER_ID,
            conversation_id=CONVERSATION_ID,
            content="show me subbhro number",
            idempotency_key=uuid4(),
            user_timezone="Asia/Kolkata",
            contacts_enabled=True,
            gmail_enabled=False,
            calendar_enabled=False,
            drive_enabled=False,
        )
        assert resp1["conversation_id"] == CONVERSATION_ID
        meta1 = resp1["assistant_message"].get("metadata") or {}
        pending = meta1.get("pending_contacts_clarification")
        assert isinstance(pending, dict), resp1["assistant_message"].get("content")
        candidates = pending.get("candidates") or []
        assert len(candidates) == 2
        assert (candidates[0].get("display_name") or "").startswith("Subbhro")

        # Reset fallback attempt counters for turn 2 clarity (same logical follow-up request identity below).
        fallback_attempts["groq"] = 0
        fallback_attempts["openrouter"] = 0
        fallback_attempts["gemini"] = 0
        tool_calls_before = dict(tool_calls)

        # Turn 2: clarification reply "1" with provider fallback during LLM execution in request.
        # Force at least one LLM call in this request so fallback chain is exercised while
        # preserving the same conversation/state/request lifecycle.
        def det_turn2(state):
            msg = (state.get("user_message") or "").strip()
            if msg == "1" and isinstance(state.get("pending_contacts_clarification"), dict):
                return None
            return _deterministic_plan_turn1(state)
        monkeypatch.setattr(gmail_agent_nodes, "_deterministic_assistant_plan", det_turn2)
        # Ensure route resolution does not bypass LLM for this short clarification reply.
        monkeypatch.setattr(gmail_agent_nodes, "_requires_intent_planning", lambda _m: True, raising=False)

        resp2 = service.send_message(
            user_id=USER_ID,
            conversation_id=CONVERSATION_ID,
            content="1",
            idempotency_key=uuid4(),
            user_timezone="Asia/Kolkata",
            contacts_enabled=True,
            gmail_enabled=False,
            calendar_enabled=False,
            drive_enabled=False,
        )

        assert resp2["conversation_id"] == CONVERSATION_ID
        # Same conversation context preserved; response is for selected first contact.
        content2 = (resp2["assistant_message"].get("content") or "")
        assert "111-111" in content2
        assert "222-222" not in content2
        # No repeated clarification
        meta2 = resp2["assistant_message"].get("metadata") or {}
        assert meta2.get("pending_contacts_clarification") is None
        # Trusted selection: get_contact should resolve selected candidate; no broad re-search fanout expected.
        assert tool_calls["get"] >= tool_calls_before.get("get", 0) + 1
        # LLM fallback exercised in this logical request (timeout, timeout, success).
        assert fallback_attempts["groq"] >= 1
        assert fallback_attempts["openrouter"] >= 1
        assert fallback_attempts["gemini"] >= 1
    finally:
        current_request_metrics.reset(token)
