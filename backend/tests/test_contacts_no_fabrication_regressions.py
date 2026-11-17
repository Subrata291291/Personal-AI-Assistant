import json
import re
from types import SimpleNamespace

import pytest

from app.workflows import contacts_read
from app.workflows.contacts_agent_node import contacts_agent_node


# Clearly marked mock contact fixtures (never live Google data).
MOCK_MAM_CONTACT = {
    "resource_name": "people/mock-mam-001",
    "display_name": "Mam",
    "primary_phone": "+8801700000001",
    "phones": [{"value": "+8801700000001", "primary": True}],
    "primary_email": "mam.mock@example.test",
    "emails": [{"value": "mam.mock@example.test", "primary": True}],
}
MOCK_HOME_BABA_CONTACT = {
    "resource_name": "people/mock-baba-001",
    "display_name": "Home Baba",
    "primary_phone": "+8801800000002",
    "phones": [{"value": "+8801800000002", "primary": True}],
}
MOCK_NO_PHONE_CONTACT = {
    "resource_name": "people/mock-nophone-001",
    "display_name": "No Phone Contact",
    "primary_email": "nophone.mock@example.test",
    "emails": [{"value": "nophone.mock@example.test", "primary": True}],
}

FABRICATED_NUMBERS = [
    "+1 (555) 123-4567",
    "+1 (555) 987-6543",
    "555-123-4567",
    "(555) 123-4567",
]


class _FakeTool:
    def __init__(self, name, handler):
        self.name = name
        self._handler = handler

    async def ainvoke(self, args):
        return self._handler(args)


class _LLMResponse:
    def __init__(self, content):
        self.content = content
        self.provider = "mock-llm"
        self.model = "mock-model"

    def completion_metadata(self):
        return {"status": "complete", "finish_reason": "stop"}


def _env_ok(data):
    return json.dumps({"ok": True, "data": data})


def _env_err(message="boom"):
    return json.dumps({"ok": False, "error": {"message": message}})


def _mock_tools(monkeypatch, handlers):
    tools = [_FakeTool(name, fn) for name, fn in handlers.items()]
    monkeypatch.setattr(contacts_read, "create_contacts_tools", lambda user_id, connection_id: tools)
    # Keep deterministic baseline stable unless a test explicitly exercises extraction.
    monkeypatch.setattr(contacts_read, "extract_contacts_read_intent_llm", lambda message: None)
    return tools


def _force_llm_fabrication(monkeypatch, content):
    """If LLM were ever consulted for contact rendering, it would fabricate. Must not affect output."""
    monkeypatch.setattr(contacts_read.llm_router, "generate", lambda req: _LLMResponse(content))


def _assert_no_fabricated_numbers(text: str):
    lowered = text.casefold()
    for num in FABRICATED_NUMBERS:
        assert num not in text
    # Generic 555 placeholder guard for common US fake patterns.
    assert "(555)" not in text
    assert "555-123" not in text
    assert "555-987" not in text


# 1) Exact contact name returns the actual Google phone number.
def test_exact_contact_name_returns_actual_google_phone_number(monkeypatch):
    def search_contacts(args):
        assert args["query"].strip().casefold() == "mam"
        return _env_ok({"contacts": [MOCK_MAM_CONTACT]})

    _mock_tools(monkeypatch, {
        "search_contacts": search_contacts,
        "list_contacts": lambda a: _env_ok({}),
        "get_contact": lambda a: _env_ok({}),
    })
    _force_llm_fabrication(monkeypatch, "Mam's number is +1 (555) 123-4567")

    result = contacts_read.generate_contacts_read_response(
        user_id="user-1",
        connection_id="conn-1",
        message="Show me Mam's number",
    )

    assert MOCK_MAM_CONTACT["primary_phone"] in result["assistant_response"]
    _assert_no_fabricated_numbers(result["assistant_response"])


# 2) Partial name with multiple matches asks for clarification.
def test_partial_name_with_multiple_matches_asks_for_clarification(monkeypatch):
    def search_contacts(args):
        return _env_ok({
            "contacts": [
                {**MOCK_HOME_BABA_CONTACT, "resource_name": "people/mock-baba-001", "display_name": "Home Baba"},
                {**MOCK_HOME_BABA_CONTACT, "resource_name": "people/mock-baba-002", "display_name": "Office Baba"},
            ]
        })

    _mock_tools(monkeypatch, {
        "search_contacts": search_contacts,
        "list_contacts": lambda a: _env_ok({}),
        "get_contact": lambda a: _env_ok({}),
    })
    _force_llm_fabrication(monkeypatch, "Home Baba is +1 (555) 987-6543 and Office Baba is +1 (555) 123-4567")

    result = contacts_read.generate_contacts_read_response(
        user_id="user-1",
        connection_id="conn-1",
        message="Show me Baba's number",
        conversation_id="conv-1",
    )

    assert result.get("pending_contacts_clarification") is not None
    assert "Home Baba" in result["assistant_response"]
    assert "Office Baba" in result["assistant_response"]
    assert "+8801800000002" not in result["assistant_response"]  # no phone/email until identified
    _assert_no_fabricated_numbers(result["assistant_response"])


# 3) Selecting a candidate returns that candidate's actual number.
def test_selecting_candidate_returns_actual_number(monkeypatch):
    pending = {
        "version": 1,
        "type": "contacts_read_clarification",
        "clarification_id": "clarify-1",
        "created_at": "2026-01-01T00:00:00+00:00",
        "expires_at": "2099-01-01T00:00:00+00:00",
        "user_id": "user-1",
        "conversation_id": "conv-1",
        "target_query": "Baba",
        "requested_attribute": "phone",
        "candidates": [
            {"resource_name": "people/mock-baba-001", "display_name": "Home Baba"},
            {"resource_name": "people/mock-baba-002", "display_name": "Office Baba"},
        ],
    }
    calls = {"get": 0}

    def get_contact(args):
        calls["get"] += 1
        assert args["resource_name"] == "people/mock-baba-001"
        return _env_ok(MOCK_HOME_BABA_CONTACT)

    _mock_tools(monkeypatch, {
        "search_contacts": lambda a: _env_ok({}),
        "list_contacts": lambda a: _env_ok({}),
        "get_contact": get_contact,
    })
    _force_llm_fabrication(monkeypatch, "Home Baba's number is +1 (555) 987-6543")

    result = contacts_read.generate_contacts_read_response(
        user_id="user-1",
        connection_id="conn-1",
        message="1",
        conversation_id="conv-1",
        pending_clarification=pending,
    )

    assert calls["get"] == 1
    assert MOCK_HOME_BABA_CONTACT["primary_phone"] in result["assistant_response"]
    assert result.get("pending_contacts_clarification") is None
    _assert_no_fabricated_numbers(result["assistant_response"])


# 4) A contact without a phone number never receives an invented number.
def test_contact_without_phone_number_never_invented(monkeypatch):
    def search_contacts(args):
        return _env_ok({"contacts": [MOCK_NO_PHONE_CONTACT]})

    _mock_tools(monkeypatch, {
        "search_contacts": search_contacts,
        "list_contacts": lambda a: _env_ok({}),
        "get_contact": lambda a: _env_ok({}),
    })
    _force_llm_fabrication(monkeypatch, "No Phone Contact's number is +1 (555) 123-4567")

    result = contacts_read.generate_contacts_read_response(
        user_id="user-1",
        connection_id="conn-1",
        message="What is No Phone Contact's phone number?",
    )

    assert "not" in result["assistant_response"].casefold() and "available" in result["assistant_response"].casefold()
    _assert_no_fabricated_numbers(result["assistant_response"])


# 5) No match never produces a fabricated number.
def test_no_match_never_produces_fabricated_number(monkeypatch):
    def search_contacts(args):
        return _env_ok({"contacts": []})

    _mock_tools(monkeypatch, {
        "search_contacts": search_contacts,
        "list_contacts": lambda a: _env_ok({}),
        "get_contact": lambda a: _env_ok({}),
    })
    _force_llm_fabrication(monkeypatch, "Mam's number is +1 (555) 123-4567")

    result = contacts_read.generate_contacts_read_response(
        user_id="user-1",
        connection_id="conn-1",
        message="Show me Mam's number",
    )

    assert "couldn" in result["assistant_response"].casefold() or "find" in result["assistant_response"].casefold()
    _assert_no_fabricated_numbers(result["assistant_response"])


# 6) Google API failure never produces a fabricated number.
def test_google_api_failure_never_produces_fabricated_number(monkeypatch):
    _mock_tools(monkeypatch, {
        "search_contacts": lambda a: _env_err("Google People API unavailable"),
        "list_contacts": lambda a: _env_ok({}),
        "get_contact": lambda a: _env_ok({}),
    })
    _force_llm_fabrication(monkeypatch, "Home Baba's number is +1 (555) 987-6543")

    result = contacts_read.generate_contacts_read_response(
        user_id="user-1",
        connection_id="conn-1",
        message="Show me Home Baba's number",
    )

    assert "couldn" in result["assistant_response"].casefold() or "try again" in result["assistant_response"].casefold()
    _assert_no_fabricated_numbers(result["assistant_response"])


# 7) A fabricated LLM phone number is rejected.
def test_fabricated_llm_phone_number_rejected(monkeypatch):
    def search_contacts(args):
        return _env_ok({"contacts": [MOCK_MAM_CONTACT]})

    _mock_tools(monkeypatch, {
        "search_contacts": search_contacts,
        "list_contacts": lambda a: _env_ok({}),
        "get_contact": lambda a: _env_ok({}),
    })
    calls = {"llm": 0}

    def fabricate(req):
        calls["llm"] += 1
        return _LLMResponse("Mam's phone number is +1 (555) 123-4567")

    monkeypatch.setattr(contacts_read.llm_router, "generate", fabricate)

    result = contacts_read.generate_contacts_read_response(
        user_id="user-1",
        connection_id="conn-1",
        message="Show me Mam's number",
    )

    assert MOCK_MAM_CONTACT["primary_phone"] in result["assistant_response"]
    assert calls["llm"] == 0
    _assert_no_fabricated_numbers(result["assistant_response"])


# 8) LLM output cannot override trusted contact data.
def test_llm_output_cannot_override_trusted_contact_data(monkeypatch):
    trusted = {
        "resource_name": "people/mock-trusted-001",
        "display_name": "Trusted Person",
        "primary_phone": "+441234567890",
        "phones": [{"value": "+441234567890", "primary": True}],
    }

    def search_contacts(args):
        return _env_ok({"contacts": [trusted]})

    _mock_tools(monkeypatch, {
        "search_contacts": search_contacts,
        "list_contacts": lambda a: _env_ok({}),
        "get_contact": lambda a: _env_ok({}),
    })
    _force_llm_fabrication(monkeypatch, "Trusted Person's phone number is +1 (555) 111-2222")

    result = contacts_read.generate_contacts_read_response(
        user_id="user-1",
        connection_id="conn-1",
        message="Show me Trusted Person's number",
    )

    assert "+441234567890" in result["assistant_response"]
    assert "+1 (555) 111-2222" not in result["assistant_response"]
    _assert_no_fabricated_numbers(result["assistant_response"])


# 9) Cross-user contact access is rejected.
def test_cross_user_pending_clarification_rejected_and_not_resolved(monkeypatch):
    pending = {
        "version": 1,
        "type": "contacts_read_clarification",
        "clarification_id": "clarify-x",
        "created_at": "2026-01-01T00:00:00+00:00",
        "expires_at": "2099-01-01T00:00:00+00:00",
        "user_id": "other-user",
        "conversation_id": "conv-1",
        "target_query": "Mam",
        "requested_attribute": "phone",
        "candidates": [
            {"resource_name": "people/other-1", "display_name": "Mam"},
        ],
    }
    calls = {"get": 0}

    def get_contact(args):
        calls["get"] += 1
        return _env_ok(MOCK_MAM_CONTACT)

    def search_contacts(args):
        # Normal lookup path for current user finds nothing; must not use other user's pending selection.
        return _env_ok({"contacts": []})

    _mock_tools(monkeypatch, {
        "search_contacts": search_contacts,
        "list_contacts": lambda a: _env_ok({}),
        "get_contact": get_contact,
    })
    _force_llm_fabrication(monkeypatch, "Mam's number is +1 (555) 123-4567")

    result = contacts_read.generate_contacts_read_response(
        user_id="user-1",
        connection_id="conn-1",
        message="Mam",
        conversation_id="conv-1",
        pending_clarification=pending,
    )

    assert calls["get"] == 0  # must not resolve/fetch using another user's trusted candidates
    assert result.get("pending_contacts_clarification") is None or (result.get("pending_contacts_clarification") or {}).get("user_id") != "other-user"
    _assert_no_fabricated_numbers(result["assistant_response"])


def test_contacts_agent_passes_authenticated_user_and_connection(monkeypatch):
    state = {
        "user_id": "user-1",
        "conversation_id": "conv-1",
        "user_message": "Show me Mam's number",
        "messages": [],
    }
    runtime = SimpleNamespace(context={
        "user_id": "user-1",
        "contacts_connection_id": "conn-1",
        "contacts_enabled": True,
        "google_connection_available": True,
    })
    seen = {}

    def fake_generate(**kwargs):
        seen.update(kwargs)
        return {
            "assistant_response": "Mam's phone number: +8801700000001",
            "provider": "",
            "model": "",
            "messages": [],
            "pending_contacts_clarification": None,
        }

    monkeypatch.setattr(
        "app.workflows.contacts_agent_node.generate_contacts_read_response",
        fake_generate,
    )
    result = contacts_agent_node(state, runtime)
    assert seen["user_id"] == "user-1"
    assert seen["connection_id"] == "conn-1"
    assert result["assistant_response"] == "Mam's phone number: +8801700000001"


# 10) Existing Gmail routing is unaffected (key Contacts-vs-Gmail boundaries).
def test_existing_gmail_routing_unaffected():
    from app.workflows.gmail_agent_nodes import _deterministic_assistant_plan

    assert _deterministic_assistant_plan({
        "user_message": "Email John about tomorrow's meeting.",
        "messages": [],
        "assistant_capabilities": {},
    })[1] == "gmail"
    assert _deterministic_assistant_plan({
        "user_message": "Show me Pihu's number",
        "messages": [],
        "assistant_capabilities": {},
    })[1] == "google_contacts"
    assert _deterministic_assistant_plan({
        "user_message": "What is John's email address?",
        "messages": [],
        "assistant_capabilities": {},
    })[1] == "google_contacts"


# 11) Existing Google Contacts read-only permissions remain unchanged.
def test_contacts_read_only_permissions_unchanged():
    from app.integrations.google.scopes import CONTACTS_READ_SCOPE, CONTACTS_WRITE_SCOPE
    from app.services.contacts_service import ContactsService

    assert ContactsService.CONTACTS_READ_SCOPE == CONTACTS_READ_SCOPE
    assert ContactsService.CONTACTS_WRITE_SCOPE == CONTACTS_WRITE_SCOPE
    # Read access is granted with read scope (and remains compatible with existing write grants by design).
    assert CONTACTS_READ_SCOPE in ContactsService.CONTACTS_READ_SCOPES
    # Chat/read workflow must not perform mutations.
    assert not hasattr(contacts_read, "create_contact") and not hasattr(contacts_read, "update_contact") and not hasattr(contacts_read, "delete_contact")


# 12) Existing contact editing/create/delete restrictions remain unchanged.
def test_contact_mutation_restrictions_remain_unchanged(monkeypatch):
    state = {
        "user_id": "user-1",
        "conversation_id": "conv-1",
        "user_message": "create a contact named Ada",
        "messages": [],
    }
    runtime = SimpleNamespace(context={
        "user_id": "user-1",
        "contacts_connection_id": "conn-1",
        "contacts_enabled": True,
        "google_connection_available": True,
    })

    result = contacts_agent_node(state, runtime)
    assert "contacts_action_proposal" not in result
    text = result["assistant_response"].casefold()
    assert "creating, updating, and deleting contacts is not available" in text
    _assert_no_fabricated_numbers(result["assistant_response"])


# --- Critical routing/follow-up regressions (must trigger Contacts on first message, no location/business clarification) ---

def test_show_me_home_baba_number_routes_directly_to_contacts():
    from app.workflows.gmail_agent_nodes import _deterministic_assistant_plan

    plan = _deterministic_assistant_plan({
        "user_message": "Show me Home Baba number.",
        "messages": [],
        "assistant_capabilities": {},
    })
    assert plan is not None
    assert plan[1] == "google_contacts"
    assert (plan[0] or {}).get("intent") == "google_contacts"


def test_contacts_search_invoked_on_first_message_and_target_extracted(monkeypatch):
    calls = {"search": 0, "query": None}

    def search_contacts(args):
        calls["search"] += 1
        calls["query"] = args.get("query")
        # Mock Google People API fixture (verified backend result)
        return _env_ok({"contacts": [MOCK_HOME_BABA_CONTACT]})

    _mock_tools(monkeypatch, {
        "search_contacts": search_contacts,
        "list_contacts": lambda a: _env_ok({}),
        "get_contact": lambda a: _env_ok({}),
    })
    _force_llm_fabrication(monkeypatch, "Home Baba's number is +1 (555) 987-6543")

    result = contacts_read.generate_contacts_read_response(
        user_id="user-1",
        connection_id="conn-1",
        message="Show me Home Baba number.",
        conversation_id="conv-home-baba",
    )

    assert calls["search"] >= 1  # Contacts lookup actually invoked on first message
    assert str(calls["query"]).casefold().strip() == "home baba"
    assert MOCK_HOME_BABA_CONTACT["primary_phone"] in result["assistant_response"]
    response_lower = result["assistant_response"].casefold()
    for banned in ("city", "region", "business type", "location", "street", "company name", "public business"):
        assert banned not in response_lower
    _assert_no_fabricated_numbers(result["assistant_response"])


def test_home_baba_followup_retains_contact_lookup_context_with_pending_clarification(monkeypatch):
    # Prior step established Contacts clarification context (trusted, user/conversation-scoped)
    pending = {
        "version": 1,
        "type": "contacts_read_clarification",
        "clarification_id": "clarify-home-baba",
        "created_at": "2026-01-01T00:00:00+00:00",
        "expires_at": "2099-01-01T00:00:00+00:00",
        "user_id": "user-1",
        "conversation_id": "conv-1",
        "target_query": "Home Baba",
        "requested_attribute": "phone",
        "candidates": [
            {"resource_name": "people/mock-baba-001", "display_name": "Home Baba"},
            {"resource_name": "people/mock-baba-002", "display_name": "Office Baba"},
        ],
    }
    calls = {"get": 0}

    def get_contact(args):
        calls["get"] += 1
        assert args["resource_name"] == "people/mock-baba-001"
        return _env_ok(MOCK_HOME_BABA_CONTACT)

    _mock_tools(monkeypatch, {
        "search_contacts": lambda a: _env_ok({}),
        "list_contacts": lambda a: _env_ok({}),
        "get_contact": get_contact,
    })
    _force_llm_fabrication(monkeypatch, "Home Baba's number is +1 (555) 987-6543")

    # "Home Baba" follow-up is interpreted as selecting the matching contact in Contacts context.
    result = contacts_read.generate_contacts_read_response(
        user_id="user-1",
        connection_id="conn-1",
        message="Home Baba",
        conversation_id="conv-1",
        pending_clarification=pending,
    )

    assert calls["get"] >= 1
    assert MOCK_HOME_BABA_CONTACT["primary_phone"] in result["assistant_response"]
    _assert_no_fabricated_numbers(result["assistant_response"])


def test_no_followup_in_clarification_context_does_not_guess_or_search_business(monkeypatch):
    from app.workflows.gmail_agent_nodes import _deterministic_assistant_plan

    pending = {
        "version": 1,
        "type": "contacts_read_clarification",
        "clarification_id": "clarify-home-baba-no",
        "created_at": "2026-01-01T00:00:00+00:00",
        "expires_at": "2099-01-01T00:00:00+00:00",
        "user_id": "user-1",
        "conversation_id": "conv-1",
        "target_query": "Home Baba",
        "requested_attribute": "phone",
        "candidates": [
            {"resource_name": "people/mock-baba-001", "display_name": "Home Baba"},
            {"resource_name": "people/mock-baba-002", "display_name": "Office Baba"},
        ],
    }

    # Routing: remains in Contacts clarification context (no Gmail/Calendar/Drive business-search route).
    plan = _deterministic_assistant_plan({
        "user_message": "No",
        "messages": [],
        "assistant_capabilities": {},
        "pending_contacts_clarification": pending,
    })
    assert plan is not None and plan[1] == "google_contacts"

    calls = {"search": 0, "get": 0}
    _mock_tools(monkeypatch, {
        "search_contacts": lambda a: (calls.__setitem__("search", calls["search"] + 1) or _env_ok({"contacts": []})),
        "list_contacts": lambda a: _env_ok({}),
        "get_contact": lambda a: (calls.__setitem__("get", calls["get"] + 1) or _env_ok(MOCK_HOME_BABA_CONTACT)),
    })
    _force_llm_fabrication(monkeypatch, "Home Baba's number is +1 (555) 987-6543")

    result = contacts_read.generate_contacts_read_response(
        user_id="user-1",
        connection_id="conn-1",
        message="No",
        conversation_id="conv-1",
        pending_clarification=pending,
    )

    # Interpreted in clarification context: does not guess/select a contact and does not fabricate.
    assert calls["get"] == 0
    assert result.get("pending_contacts_clarification") is not None
    response_lower = result["assistant_response"].casefold()
    for banned in ("city", "region", "business type", "public business"):
        assert banned not in response_lower
    _assert_no_fabricated_numbers(result["assistant_response"])


def test_api_failure_never_triggers_public_business_search(monkeypatch):
    _mock_tools(monkeypatch, {
        "search_contacts": lambda a: _env_err("Google People API unavailable"),
        "list_contacts": lambda a: _env_ok({}),
        "get_contact": lambda a: _env_ok({}),
    })
    _force_llm_fabrication(monkeypatch, "Try searching businesses near you for Home Baba: +1 (555) 987-6543")

    result = contacts_read.generate_contacts_read_response(
        user_id="user-1",
        connection_id="conn-1",
        message="Show me Home Baba number.",
        conversation_id="conv-home-baba",
    )

    response_lower = result["assistant_response"].casefold()
    assert "couldn" in response_lower or "try again" in response_lower or "unable" in response_lower
    for banned in ("city", "region", "business type", "near you", "public business", "business search"):
        assert banned not in response_lower
    _assert_no_fabricated_numbers(result["assistant_response"])


# --- Conversational correction / exclusion regressions ---

MOCK_MAM_HEART_CONTACT = {
    "resource_name": "people/mock-mam-heart-001",
    "display_name": "Mam ❤️",
    "primary_phone": "+8801700000099",
    "phones": [{"value": "+8801700000099", "primary": True}],
}
MOCK_BORO_MAMI_CONTACT = {
    "resource_name": "people/mock-boro-mami-001",
    "display_name": "Mams (Boro Mams)",
    "primary_phone": "+8801700000011",
    "phones": [{"value": "+8801700000011", "primary": True}],
    "primary_email": "boro.mams@example.com",
    "emails": [{"value": "boro.mams@example.com", "primary": True}],
}


def test_mams_initial_query_with_multiple_matches_asks_for_clarification(monkeypatch):
    calls = {"search": 0}

    def search_contacts(args):
        calls["search"] += 1
        return _env_ok({"contacts": [MOCK_MAM_HEART_CONTACT, MOCK_BORO_MAMI_CONTACT]})

    _mock_tools(monkeypatch, {
        "search_contacts": search_contacts,
        "list_contacts": lambda a: _env_ok({}),
        "get_contact": lambda a: _env_ok({}),
    })

    result = contacts_read.generate_contacts_read_response(
        user_id="user-1",
        connection_id="conn-1",
        message="Can you check mams number?",
        conversation_id="conv-correction-1",
    )

    assert calls["search"] >= 1  # actual People API search tool invoked
    pending = result.get("pending_contacts_clarification")
    assert pending is not None
    assert pending["conversation_id"] == "conv-correction-1"
    assert pending["user_id"] == "user-1"
    response = result["assistant_response"]
    # Must not silently select wrong/unrelated contact or expose fabricated/unguarded details.
    assert "+8801700000099" not in response and "+8801700000011" not in response
    assert "boro.mams@example.com" not in response
    assert "Mam" in response or "Boro" in response or "which contact" in response.casefold()


def test_correction_excludes_rejected_candidate_and_searches_corrected_target(monkeypatch):
    pending = {
        "version": 1,
        "type": "contacts_read_clarification",
        "clarification_id": "clarify-corr-1",
        "created_at": "2026-01-01T00:00:00+00:00",
        "expires_at": "2099-01-01T00:00:00+00:00",
        "user_id": "user-1",
        "conversation_id": "conv-correction-1",
        "target_query": "mams",
        "requested_attribute": "phone",
        "candidates": [
            {"resource_name": MOCK_MAM_HEART_CONTACT["resource_name"], "display_name": MOCK_MAM_HEART_CONTACT["display_name"]},
            {"resource_name": MOCK_BORO_MAMI_CONTACT["resource_name"], "display_name": MOCK_BORO_MAMI_CONTACT["display_name"]},
        ],
    }
    calls = {"search": 0, "queries": [], "get": 0}

    def search_contacts(args):
        calls["search"] += 1
        calls["queries"].append(args.get("query"))
        # Corrected search for Mam; backend may still return both, but rejected candidate must be excluded.
        return _env_ok({"contacts": [MOCK_MAM_HEART_CONTACT, MOCK_BORO_MAMI_CONTACT]})

    def get_contact(args):
        calls["get"] += 1
        return _env_ok(MOCK_MAM_HEART_CONTACT if args["resource_name"] == MOCK_MAM_HEART_CONTACT["resource_name"] else MOCK_BORO_MAMI_CONTACT)

    _mock_tools(monkeypatch, {
        "search_contacts": search_contacts,
        "list_contacts": lambda a: _env_ok({}),
        "get_contact": get_contact,
    })

    result = contacts_read.generate_contacts_read_response(
        user_id="user-1",
        connection_id="conn-1",
        message="Not Boro Mami, I want Mam number.",
        conversation_id="conv-correction-1",
        pending_clarification=pending,
    )

    assert calls["search"] >= 1  # actual search/tool invocation for corrected target
    assert all("not boro mami, i want mam" not in str(q).casefold() for q in calls["queries"])
    assert any("mam" in str(q).casefold() for q in calls["queries"])
    response = result["assistant_response"]
    assert "+8801700000099" in response  # trusted verified phone from People API result
    assert "+8801700000011" not in response  # rejected candidate excluded
    assert "boro.mams@example.com" not in response  # never expose unrelated/fabricated email in phone lookup
    assert result.get("pending_contacts_clarification") is None


def test_correction_missing_phone_reported_accurately(monkeypatch):
    pending = {
        "version": 1,
        "type": "contacts_read_clarification",
        "clarification_id": "clarify-corr-2",
        "created_at": "2026-01-01T00:00:00+00:00",
        "expires_at": "2099-01-01T00:00:00+00:00",
        "user_id": "user-1",
        "conversation_id": "conv-correction-2",
        "target_query": "mams",
        "requested_attribute": "phone",
        "candidates": [
            {"resource_name": "people/mock-mam-nophone-001", "display_name": "Mam ❤️"},
            {"resource_name": MOCK_BORO_MAMI_CONTACT["resource_name"], "display_name": MOCK_BORO_MAMI_CONTACT["display_name"]},
        ],
    }
    no_phone_mam = {**MOCK_MAM_HEART_CONTACT, "resource_name": "people/mock-mam-nophone-001", "primary_phone": None, "phones": []}

    def search_contacts(args):
        return _env_ok({"contacts": [no_phone_mam, MOCK_BORO_MAMI_CONTACT]})

    _mock_tools(monkeypatch, {
        "search_contacts": search_contacts,
        "list_contacts": lambda a: _env_ok({}),
        "get_contact": lambda a: _env_ok(no_phone_mam),
    })

    result = contacts_read.generate_contacts_read_response(
        user_id="user-1",
        connection_id="conn-1",
        message="Not Boro Mami, I want Mam number.",
        conversation_id="conv-correction-2",
        pending_clarification=pending,
    )

    text = result["assistant_response"].casefold()
    assert "available" in text or "doesn" in text or "does not" in text or "not" in text
    assert "+8801700000011" not in result["assistant_response"]
    _assert_no_fabricated_numbers(result["assistant_response"])


def test_correction_api_failure_does_not_fabricate(monkeypatch):
    pending = {
        "version": 1,
        "type": "contacts_read_clarification",
        "clarification_id": "clarify-corr-3",
        "created_at": "2026-01-01T00:00:00+00:00",
        "expires_at": "2099-01-01T00:00:00+00:00",
        "user_id": "user-1",
        "conversation_id": "conv-correction-3",
        "target_query": "mams",
        "requested_attribute": "phone",
        "candidates": [
            {"resource_name": MOCK_MAM_HEART_CONTACT["resource_name"], "display_name": "Mam ❤️"},
            {"resource_name": MOCK_BORO_MAMI_CONTACT["resource_name"], "display_name": "Boro Mami"},
        ],
    }

    _mock_tools(monkeypatch, {
        "search_contacts": lambda a: _env_err("Google People API unavailable"),
        "list_contacts": lambda a: _env_ok({}),
        "get_contact": lambda a: _env_ok({}),
    })

    result = contacts_read.generate_contacts_read_response(
        user_id="user-1",
        connection_id="conn-1",
        message="Not Boro Mami, I want Mam number.",
        conversation_id="conv-correction-3",
        pending_clarification=pending,
    )

    response_lower = result["assistant_response"].casefold()
    assert "couldn" in response_lower or "try again" in response_lower or "unable" in response_lower
    assert "+8801700000099" not in result["assistant_response"]
    assert "+8801700000011" not in result["assistant_response"]
    assert "boro.mams@example.com" not in result["assistant_response"]
    _assert_no_fabricated_numbers(result["assistant_response"])


def test_correction_is_user_and_conversation_scoped(monkeypatch):
    pending = {
        "version": 1,
        "type": "contacts_read_clarification",
        "clarification_id": "clarify-corr-4",
        "created_at": "2026-01-01T00:00:00+00:00",
        "expires_at": "2099-01-01T00:00:00+00:00",
        "user_id": "other-user",
        "conversation_id": "other-conv",
        "target_query": "mams",
        "requested_attribute": "phone",
        "candidates": [
            {"resource_name": MOCK_MAM_HEART_CONTACT["resource_name"], "display_name": "Mam ❤️"},
            {"resource_name": MOCK_BORO_MAMI_CONTACT["resource_name"], "display_name": "Boro Mami"},
        ],
        "rejected_resource_names": [MOCK_BORO_MAMI_CONTACT["resource_name"]],
    }
    calls = {"get": 0, "search": 0}

    def get_contact(args):
        calls["get"] += 1
        return _env_ok(MOCK_MAM_HEART_CONTACT)

    def search_contacts(args):
        calls["search"] += 1
        return _env_ok({"contacts": []})

    _mock_tools(monkeypatch, {
        "search_contacts": search_contacts,
        "list_contacts": lambda a: _env_ok({}),
        "get_contact": get_contact,
    })

    result = contacts_read.generate_contacts_read_response(
        user_id="user-1",
        connection_id="conn-1",
        message="Not Boro Mami, I want Mam number.",
        conversation_id="conv-correction-1",
        pending_clarification=pending,
    )

    assert calls["get"] == 0  # must not fetch/resolve using another user's trusted clarification state
    pending_out = result.get("pending_contacts_clarification")
    assert pending_out is None or (
        pending_out.get("user_id") == "user-1" and pending_out.get("conversation_id") == "conv-correction-1"
    )
    _assert_no_fabricated_numbers(result["assistant_response"])


def test_check_rintus_number_returns_exact_verified_google_fields(monkeypatch):
    """Exact request regression: prove returned phone comes from verified Google People API result (no fabrication).

    For a partial/non-exact name variant, confirmation may be required before selecting; the verified
    Google People API contact details must still be the only source when returned.
    """
    calls = {"search": 0, "queries": [], "get": 0}
    google_contact = {
        "resource_name": "people/rintu-verified-001",
        "display_name": "Rintu",
        "primary_phone": "+91 91234 56789",
        "phones": [{"value": "+91 91234 56789", "primary": True}],
        "primary_email": "rintu.verified@trusted.example.test",
        "emails": [{"value": "rintu.verified@trusted.example.test", "primary": True}],
    }

    def search_contacts(args):
        calls["search"] += 1
        calls["queries"].append(args.get("query"))
        # Mocked Google People API (verified backend source) result
        return _env_ok({"contacts": [google_contact]})

    def get_contact(args):
        calls["get"] += 1
        assert args.get("resource_name") == google_contact["resource_name"]
        return _env_ok(google_contact)

    _mock_tools(monkeypatch, {
        "search_contacts": search_contacts,
        "list_contacts": lambda a: _env_ok({}),
        "get_contact": get_contact,
    })
    # If LLM were consulted for contact rendering, it would fabricate; must not affect output.
    _force_llm_fabrication(monkeypatch, "Name: Rintu Phone: +91 98765 12345 Email: rintu@example.com")

    result = contacts_read.generate_contacts_read_response(
        user_id="user-1",
        connection_id="conn-1",
        message="check rintus number",
        conversation_id="conv-rintu-1",
    )

    assert calls["search"] >= 1  # actual Google People API search tool invoked
    assert any("rintu" in str(q).casefold() for q in calls["queries"])
    response = result["assistant_response"]
    # Must never fabricate placeholder values from the reproduction.
    assert "+91 98765 12345" not in response
    assert "98765 12345" not in response
    assert "rintu@example.com" not in response
    _assert_no_fabricated_numbers(response)

    if result.get("pending_contacts_clarification"):
        # Partial/non-exact match: confirmation required, no phone exposed yet.
        assert "+91 91234 56789" not in response
        assert "rintu.verified@trusted.example.test" not in response
        pending = result["pending_contacts_clarification"]
        assert pending["user_id"] == "user-1" and pending["conversation_id"] == "conv-rintu-1"
        # User confirms/selects the trusted candidate; returned details must come from verified Google People API result.
        result2 = contacts_read.generate_contacts_read_response(
            user_id="user-1",
            connection_id="conn-1",
            message="Rintu",
            conversation_id="conv-rintu-1",
            pending_clarification=pending,
        )
        response2 = result2["assistant_response"]
        assert "+91 91234 56789" in response2  # exact verified Google People API phone value
        assert "rintu.verified@trusted.example.test" not in response2  # phone lookup, no fabricated/injected email
        assert "+91 98765 12345" not in response2 and "98765 12345" not in response2
        assert "rintu@example.com" not in response2
        assert result2.get("pending_contacts_clarification") is None
        _assert_no_fabricated_numbers(response2)
    else:
        # If treated as a unique trustworthy match, exact verified phone must be returned directly.
        assert "+91 91234 56789" in response
        assert "rintu.verified@trusted.example.test" not in response
        assert result.get("pending_contacts_clarification") is None
        _assert_no_fabricated_numbers(response)


def test_exact_mams_correction_conversation_triggers_second_verified_search(monkeypatch):
    """Exact reported conversation: turn 1 asks clarification, turn 2 correction excludes rejected candidate and re-searches."""
    mam = {
        "resource_name": "people/mam-target-001",
        "display_name": "Mam",
        "primary_phone": "+8801700000099",
        "phones": [{"value": "+8801700000099", "primary": True}],
    }
    boro_mams = {
        "resource_name": "people/boro-mams-001",
        "display_name": "Mams (Boro Mams)",
        "primary_phone": "+8801700000011",
        "phones": [{"value": "+8801700000011", "primary": True}],
    }
    calls = {"search": [], "get": [], "tool_user_ids": [], "tool_connection_ids": []}

    def search_contacts(args):
        calls["search"].append(dict(args))
        # Mocked Google People API search results for authenticated user's selected connection
        return _env_ok({"contacts": [mam, boro_mams]})

    def get_contact(args):
        calls["get"].append(dict(args))
        if args.get("resource_name") == mam["resource_name"]:
            return _env_ok(mam)
        return _env_ok(boro_mams)

    # Capture authenticated user/connection passed to tools (proves correct Google connection context used by workflow)
    real_create = contacts_read.create_contacts_tools
    def create_tools_capture(user_id, connection_id):
        calls["tool_user_ids"].append(user_id)
        calls["tool_connection_ids"].append(connection_id)
        return [
            _FakeTool("search_contacts", search_contacts),
            _FakeTool("list_contacts", lambda a: _env_ok({})),
            _FakeTool("get_contact", get_contact),
        ]
    monkeypatch.setattr(contacts_read, "create_contacts_tools", create_tools_capture)
    monkeypatch.setattr(contacts_read, "extract_contacts_read_intent_llm", lambda message: None)

    # Turn 1: "Can you check mams number?" -> actual search invoked, multiple matches => clarification (no silent selection)
    first = contacts_read.generate_contacts_read_response(
        user_id="user-1",
        connection_id="conn-1",
        message="Can you check mams number?",
        conversation_id="conv-mams-1",
    )
    assert len(calls["search"]) >= 1
    pending = first.get("pending_contacts_clarification")
    assert pending is not None
    assert pending["user_id"] == "user-1" and pending["conversation_id"] == "conv-mams-1"
    first_resp = first["assistant_response"]
    assert "+8801700000099" not in first_resp and "+8801700000011" not in first_resp
    assert "Mam" in first_resp or "Boro" in first_resp or "which contact" in first_resp.casefold()

    # Turn 2 correction: must exclude rejected candidate and perform a second corrected Google People API search
    second = contacts_read.generate_contacts_read_response(
        user_id="user-1",
        connection_id="conn-1",
        message="Not Boro Mami, I want Mam number.",
        conversation_id="conv-mams-1",
        pending_clarification=pending,
    )
    assert len(calls["search"]) >= 2  # second search after correction
    corrected_query = str(calls["search"][-1].get("query", "")).casefold()
    assert "not boro mami, i want mam" not in corrected_query
    assert "mam" in corrected_query
    # Authenticated user + selected Google connection context used for tool creation
    assert all(uid == "user-1" for uid in calls["tool_user_ids"])
    assert all(cid == "conn-1" for cid in calls["tool_connection_ids"])
    resp = second["assistant_response"]
    assert "+8801700000099" in resp  # verified Google People API value for intended contact
    assert "+8801700000011" not in resp  # rejected candidate excluded
    assert "Mams (Boro Mams)" not in resp or "Mam" in resp
    assert second.get("pending_contacts_clarification") is None
    _assert_no_fabricated_numbers(resp)



def test_first_turn_check_mams_number_multiple_matches_requests_clarification_without_details(monkeypatch):
    """Exact first-turn query: multiple matches must request clarification and expose no phone/email or stale details."""
    calls = {"search": 0, "get": 0}
    a = {
        "resource_name": "people/mam-a",
        "display_name": "Mam A",
        "primary_phone": "+8801700000001",
        "phones": [{"value": "+8801700000001", "primary": True}],
        "primary_email": "mama@example.test",
        "emails": [{"value": "mama@example.test", "primary": True}],
    }
    b = {
        "resource_name": "people/mam-b",
        "display_name": "Mams (Boro Mams)",
        "primary_phone": "+8801700000002",
        "phones": [{"value": "+8801700000002", "primary": True}],
        "primary_email": "mamb@example.test",
        "emails": [{"value": "mamb@example.test", "primary": True}],
    }

    def search_contacts(args):
        calls["search"] += 1
        return _env_ok({"contacts": [a, b]})

    def get_contact(args):
        calls["get"] += 1
        return _env_ok(a if args.get("resource_name") == a["resource_name"] else b)

    _mock_tools(monkeypatch, {
        "search_contacts": search_contacts,
        "list_contacts": lambda _a: _env_ok({}),
        "get_contact": get_contact,
    })
    monkeypatch.setattr(contacts_read, "extract_contacts_read_intent_llm", lambda message: None)

    result = contacts_read.generate_contacts_read_response(
        user_id="user-1",
        connection_id="conn-1",
        message="Can you check mams number?",
        conversation_id="conv-mams-first-turn",
    )

    assert calls["search"] >= 1  # Google People API search tool actually invoked
    assert calls["get"] == 0  # must not auto-select/fetch a contact detail when ambiguous
    response = result["assistant_response"]
    pending = result.get("pending_contacts_clarification")
    assert pending is not None
    assert pending["user_id"] == "user-1"
    assert pending["conversation_id"] == "conv-mams-first-turn"
    # No phone numbers or emails from any candidate/current/previous request in clarification
    assert "+8801700000001" not in response
    assert "+8801700000002" not in response
    assert "1700000001" not in response
    assert "1700000002" not in response
    assert "mama@example.test" not in response
    assert "mamb@example.test" not in response
    assert "rintu@example.com" not in response
    assert "+91 98765 67890" not in response
    assert "98765 67890" not in response
    assert re.search(r"which contact|confirm|reply with the number|multiple", response, re.IGNORECASE)
    _assert_no_fabricated_numbers(response)


def test_stale_contact_result_from_other_user_or_conversation_cannot_be_rendered(monkeypatch):
    """A stale/trusted-looking clarification or cached context from another user/conversation must not render contact details."""
    stale_contact = {
        "resource_name": "people/stale-001",
        "display_name": "Stale Person",
        "primary_phone": "+8801999999999",
        "phones": [{"value": "+8801999999999", "primary": True}],
    }
    other_user_pending = {
        "version": 1,
        "type": "contacts_read_clarification",
        "clarification_id": "clarify-stale-user",
        "created_at": "2026-01-01T00:00:00+00:00",
        "expires_at": "2099-01-01T00:00:00+00:00",
        "user_id": "other-user",
        "conversation_id": "conv-current",
        "target_query": "mams",
        "requested_attribute": "phone",
        "candidates": [{"resource_name": stale_contact["resource_name"], "display_name": "Stale Person"}],
    }
    other_conv_pending = {**other_user_pending, "clarification_id": "clarify-stale-conv", "user_id": "user-1", "conversation_id": "conv-other"}

    calls = {"get": 0, "search": 0}

    def get_contact(args):
        calls["get"] += 1
        return _env_ok(stale_contact)

    def search_contacts(args):
        calls["search"] += 1
        # Fresh current request returns multiple matching "Mam" contacts (ambiguous) from mocked verified People API results.
        return _env_ok({"contacts": [
            {"resource_name": "people/mam-x", "display_name": "Mam", "primary_phone": "+8801700000001", "phones": [{"value": "+8801700000001", "primary": True}]},
            {"resource_name": "people/mam-y", "display_name": "Mams (Boro Mams)", "primary_phone": "+8801700000002", "phones": [{"value": "+8801700000002", "primary": True}]},
        ]})

    _mock_tools(monkeypatch, {
        "search_contacts": search_contacts,
        "list_contacts": lambda _a: _env_ok({}),
        "get_contact": get_contact,
    })
    monkeypatch.setattr(contacts_read, "extract_contacts_read_intent_llm", lambda message: None)

    # Try to resolve/select using stale pending state from another user
    res_other_user = contacts_read.generate_contacts_read_response(
        user_id="user-1",
        connection_id="conn-1",
        message="1",
        conversation_id="conv-current",
        pending_clarification=other_user_pending,
    )
    assert calls["get"] == 0  # must not fetch stale trusted candidate from another user
    assert "+8801999999999" not in res_other_user["assistant_response"]

    # Try to resolve/select using stale pending state from another conversation
    res_other_conv = contacts_read.generate_contacts_read_response(
        user_id="user-1",
        connection_id="conn-1",
        message="Stale Person",
        conversation_id="conv-current",
        pending_clarification=other_conv_pending,
    )
    assert calls["get"] == 0
    assert "+8801999999999" not in res_other_conv["assistant_response"]

    # Fresh request in current user/conversation must not inherit stale details and must require clarification (no numbers exposed)
    fresh = contacts_read.generate_contacts_read_response(
        user_id="user-1",
        connection_id="conn-1",
        message="Can you check mams number?",
        conversation_id="conv-current",
    )
    assert calls["search"] >= 1
    assert "+8801999999999" not in fresh["assistant_response"]
    assert fresh.get("pending_contacts_clarification") is not None
    assert fresh["pending_contacts_clarification"]["user_id"] == "user-1"
    assert fresh["pending_contacts_clarification"]["conversation_id"] == "conv-current"
    assert "+8801700000001" not in fresh["assistant_response"]
    assert "+8801700000002" not in fresh["assistant_response"]
    assert "1999999999" not in fresh["assistant_response"]
    _assert_no_fabricated_numbers(fresh["assistant_response"])


def test_show_me_rintus_number_normalizes_possessive_when_api_results_support_it(monkeypatch):
    calls = {"search": [], "get": []}
    rintu = {
        "resource_name": "people/rintu-1",
        "display_name": "Rintu",
        "primary_phone": "+8801711111111",
        "phones": [{"value": "+8801711111111", "primary": True}],
        "primary_email": "rintu@example.test",
        "emails": [{"value": "rintu@example.test", "primary": True}],
    }

    def search_contacts(args):
        calls["search"].append(dict(args))
        # Target remains "rintus" (genuine names ending in s not stripped); initial query returns nothing,
        # conservative fallback query "rintu" returns actual API-backed contact supporting normalization.
        if (args.get("query") or "").casefold() == "rintus":
            return _env_ok({"contacts": []})
        if (args.get("query") or "").casefold() == "rintu":
            return _env_ok({"contacts": [rintu]})
        return _env_ok({"contacts": []})

    def get_contact(args):
        calls["get"].append(dict(args))
        return _env_ok(rintu)

    _mock_tools(monkeypatch, {
        "search_contacts": search_contacts,
        "list_contacts": lambda _a: _env_ok({}),
        "get_contact": get_contact,
    })
    monkeypatch.setattr(contacts_read, "extract_contacts_read_intent_llm", lambda message: None)

    result = contacts_read.generate_contacts_read_response(
        user_id="user-1",
        connection_id="conn-1",
        message="show me rintus number",
        conversation_id="conv-rintu",
    )

    assert [c["query"] for c in calls["search"]] == ["rintus", "rintu"]  # actual mocked search invocations (fallback uses real API results)
    resp = result["assistant_response"]
    prov = result.get("contacts_provenance") or {}
    # Normalization is applied locally over actual returned contacts; single partial/normalized match may still
    # require confirmation (no silent fuzzy auto-selection), but must not fabricate or expose unverified details.
    if prov.get("outcome_status") == "single_match":
        assert resp == "Rintu's phone number: +8801711111111"
        assert prov.get("verified_source") is True
        assert prov.get("selected_contact_resource_ref") == contacts_read._safe_ref("people/rintu-1")
        assert prov.get("displayed_phone_from_validated_api") is True
    else:
        assert prov.get("outcome_status") == "multiple_matches"
        assert "Rintu" in resp
        assert "+8801711111111" not in resp
        assert "rintu@example.test" not in resp
        assert result.get("pending_contacts_clarification") is not None
    _assert_no_fabricated_numbers(resp)


def test_show_me_rintus_number_extracts_rintus_target_without_truncating_genuine_names():
    intent = contacts_read.extract_contacts_read_intent("show me rintus number")
    assert intent.intent == "get_contact_phone"
    assert intent.requested_attribute == "phone"
    # Possessive-like apostrophe-less form is preserved as extracted target; normalization is applied safely via fallback search/matching.
    assert (intent.target_query or "").casefold() == "rintus"


def test_show_me_rintu_possessive_number_extracts_rintu():
    intent = contacts_read.extract_contacts_read_intent("show me Rintu's number")
    assert intent.intent == "get_contact_phone"
    assert (intent.target_query or "").casefold() == "rintu"


def test_genuine_contact_name_ending_with_s_not_truncated(monkeypatch):
    calls = {"search": []}
    james = {
        "resource_name": "people/james-1",
        "display_name": "James",
        "primary_phone": "+8801722222222",
        "phones": [{"value": "+8801722222222", "primary": True}],
    }

    def search_contacts(args):
        calls["search"].append(dict(args))
        assert args.get("query") == "James"  # must not be truncated to "Jame"
        return _env_ok({"contacts": [james]})

    _mock_tools(monkeypatch, {
        "search_contacts": search_contacts,
        "list_contacts": lambda _a: _env_ok({}),
        "get_contact": lambda _a: _env_ok(james),
    })
    monkeypatch.setattr(contacts_read, "extract_contacts_read_intent_llm", lambda message: None)

    intent = contacts_read.extract_contacts_read_intent("show me James number")
    assert (intent.target_query or "") == "James"
    result = contacts_read.generate_contacts_read_response(
        user_id="user-1",
        connection_id="conn-1",
        message="show me James number",
        conversation_id="conv-james",
    )
    assert result["assistant_response"] == "James's phone number: +8801722222222"
    assert [c["query"] for c in calls["search"]][0] == "James"
    assert "Jame" not in [c["query"] for c in calls["search"]]  # no incorrect truncation


def test_similar_contacts_after_normalization_trigger_clarification(monkeypatch):
    calls = {"search": []}
    rintu = {"resource_name": "people/rintu-a", "display_name": "Rintu", "primary_phone": "+8801711111111", "phones": [{"value": "+8801711111111", "primary": True}]}
    rintu_office = {"resource_name": "people/rintu-b", "display_name": "Rintu Office", "primary_phone": "+8801733333333", "phones": [{"value": "+8801733333333", "primary": True}]}

    def search_contacts(args):
        calls["search"].append(dict(args))
        if (args.get("query") or "").casefold() == "rintus":
            return _env_ok({"contacts": []})
        if (args.get("query") or "").casefold() == "rintu":
            return _env_ok({"contacts": [rintu, rintu_office]})
        return _env_ok({"contacts": []})

    _mock_tools(monkeypatch, {
        "search_contacts": search_contacts,
        "list_contacts": lambda _a: _env_ok({}),
        "get_contact": lambda _a: _env_ok(rintu),
    })
    monkeypatch.setattr(contacts_read, "extract_contacts_read_intent_llm", lambda message: None)

    result = contacts_read.generate_contacts_read_response(
        user_id="user-1",
        connection_id="conn-1",
        message="show me rintus number",
        conversation_id="conv-rintu-amb",
    )
    assert [c["query"] for c in calls["search"]] == ["rintus", "rintu"]
    assert result.get("pending_contacts_clarification") is not None
    assert "+8801711111111" not in result["assistant_response"]
    assert "+8801733333333" not in result["assistant_response"]
    assert re.search(r"which contact|confirm|reply with the number|multiple", result["assistant_response"], re.IGNORECASE)
    _assert_no_fabricated_numbers(result["assistant_response"])


def test_no_matching_contact_after_normalized_fallback_returns_safe_no_match(monkeypatch):
    calls = {"search": []}

    def search_contacts(args):
        calls["search"].append(dict(args))
        return _env_ok({"contacts": []})

    _mock_tools(monkeypatch, {
        "search_contacts": search_contacts,
        "list_contacts": lambda _a: _env_ok({}),
        "get_contact": lambda _a: _env_ok({}),
    })
    monkeypatch.setattr(contacts_read, "extract_contacts_read_intent_llm", lambda message: None)

    result = contacts_read.generate_contacts_read_response(
        user_id="user-1",
        connection_id="conn-1",
        message="show me rintus number",
        conversation_id="conv-rintu-none",
    )
    assert [c["query"] for c in calls["search"]] == ["rintus", "rintu"]
    assert "couldn" in result["assistant_response"].casefold() or "could not find" in result["assistant_response"].casefold()
    assert "+8801" not in result["assistant_response"]
    assert "@" not in result["assistant_response"]
    prov = result.get("contacts_provenance") or {}
    assert prov.get("outcome_status") in {"no_match", "incomplete_search"}
    assert prov.get("selected_contact_resource_ref") is None
    _assert_no_fabricated_numbers(result["assistant_response"])

