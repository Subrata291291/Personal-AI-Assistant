import json
from types import SimpleNamespace

import pytest

from app.workflows.contacts_read import (
    ContactsReadIntent,
    extract_contacts_read_intent,
    generate_contacts_read_response,
)
from app.workflows import contacts_read
from app.workflows.contacts_agent_node import contacts_agent_node


class _FakeTool:
    def __init__(self, name, handler):
        self.name = name
        self._handler = handler

    async def ainvoke(self, args):
        return self._handler(args)


def _env_ok(data):
    return json.dumps({"ok": True, "data": data})


def _env_err(message="boom"):
    return json.dumps({"ok": False, "error": {"message": message}})


def _mock_tools(monkeypatch, handlers):
    tools = [_FakeTool(name, fn) for name, fn in handlers.items()]
    monkeypatch.setattr(
        contacts_read,
        "create_contacts_tools",
        lambda user_id, connection_id: tools,
    )
    # Default: disable LLM extraction so Phase 3B-style deterministic flow is exercised
    # unless a test explicitly overrides extraction.
    monkeypatch.setattr(contacts_read, "extract_contacts_read_intent_llm", lambda message: None)
    return tools


class _LLMResponse:
    def __init__(self, content="grounded answer", provider="test-provider", model="test-model"):
        self.content = content
        self.provider = provider
        self.model = model

    def completion_metadata(self):
        return {"status": "complete", "finish_reason": "stop"}


def _state_and_runtime(message, *, connection_id="conn-1", write_connection_id="conn-1", enabled=True, selection_required=False):
    state = {
        "user_id": "user-1",
        "conversation_id": "conv-1",
        "user_message": message,
        "messages": [],
    }
    runtime = SimpleNamespace(
        context={
            "user_id": "user-1",
            "contacts_connection_id": connection_id,
            "contacts_write_connection_id": write_connection_id,
            "contacts_enabled": enabled,
            "contacts_selection_required": selection_required,
            "contacts_candidate_emails": [],
            "contacts_read_scope_missing": False,
            "contacts_write_scope_missing": False,
        }
    )
    return state, runtime


# --- Intent and tool selection ---

def test_intent_list_contacts():
    intent = extract_contacts_read_intent("show me all contacts")
    assert intent.intent == "list_contacts"
    assert intent.requested_attribute == "none"


def test_intent_count_contacts():
    intent = extract_contacts_read_intent("how many contacts do I have?")
    assert intent.intent == "count_contacts"
    assert intent.requested_attribute == "none"


def test_intent_phone_lookup_not_list():
    intent = extract_contacts_read_intent("show me the contact no of rintu")
    assert intent.intent == "get_contact_phone"
    assert intent.requested_attribute == "phone"
    assert intent.target_query and "rintu" in intent.target_query.casefold()


def test_intent_phone_question():
    intent = extract_contacts_read_intent("what is Rintu's phone number?")
    assert intent.intent == "get_contact_phone"
    assert intent.requested_attribute == "phone"
    assert intent.target_query and "rintu" in intent.target_query.casefold()


def test_intent_email_lookup():
    intent = extract_contacts_read_intent("find Rahul's email")
    assert intent.intent == "get_contact_email"
    assert intent.requested_attribute == "email"
    assert intent.target_query and "rahul" in intent.target_query.casefold()


def test_intent_contact_details():
    intent = extract_contacts_read_intent("give me Rintu's contact details")
    assert intent.intent == "get_contact_details"
    assert intent.requested_attribute == "details"
    assert intent.target_query and "rintu" in intent.target_query.casefold()


def test_intent_search_named_contact():
    intent = extract_contacts_read_intent("search contact named Rintu")
    assert intent.intent == "search_contact"
    assert intent.target_query and "rintu" in intent.target_query.casefold()


# --- Result handling and grounded generation ---

def test_single_match_with_phone_uses_search_and_trusted_render(monkeypatch):
    calls = {"search": None, "llm": 0}

    def search_contacts(args):
        calls["search"] = args
        return _env_ok({
            "contacts": [{
                "resource_name": "people/1",
                "display_name": "Rintu",
                "primary_phone": "+8801700000000",
                "phones": [{"value": "+8801700000000", "primary": True}],
            }]
        })

    def get_contact(args):
        return _env_ok({
            "resource_name": "people/1",
            "display_name": "Rintu",
            "primary_phone": "+8801700000000",
            "phones": [{"value": "+8801700000000", "primary": True}],
        })

    _mock_tools(monkeypatch, {"search_contacts": search_contacts, "list_contacts": lambda a: _env_ok({}), "get_contact": get_contact})

    def fake_generate(req):
        calls["llm"] += 1
        return _LLMResponse(content="FABRICATED +1 (555) 000-0000")

    monkeypatch.setattr(contacts_read.llm_router, "generate", fake_generate)

    result = generate_contacts_read_response(
        user_id="user-1", connection_id="conn-1", message="show me the contact no of rintu"
    )

    assert calls["search"]["query"].casefold().strip() == "rintu"
    assert "8801700000000" in result["assistant_response"]
    assert "555" not in result["assistant_response"]
    assert calls["llm"] == 0  # contact value must come from trusted backend data, not LLM


def test_single_match_with_email(monkeypatch):
    def search_contacts(args):
        return _env_ok({
            "contacts": [{
                "resource_name": "people/2",
                "display_name": "Rahul",
                "primary_email": "rahul@example.com",
                "emails": [{"value": "rahul@example.com", "primary": True}],
            }]
        })

    def get_contact(args):
        return _env_ok({
            "resource_name": "people/2",
            "display_name": "Rahul",
            "primary_email": "rahul@example.com",
            "emails": [{"value": "rahul@example.com", "primary": True}],
        })

    _mock_tools(monkeypatch, {"search_contacts": search_contacts, "list_contacts": lambda a: _env_ok({}), "get_contact": get_contact})
    monkeypatch.setattr(contacts_read.llm_router, "generate", lambda req: _LLMResponse("Rahul's email is rahul@example.com."))

    result = generate_contacts_read_response(
        user_id="user-1", connection_id="conn-1", message="find Rahul's email"
    )
    assert "rahul@example.com" in result["assistant_response"]


def test_contact_without_requested_phone(monkeypatch):
    captured = {}

    def search_contacts(args):
        return _env_ok({
            "contacts": [{
                "resource_name": "people/3",
                "display_name": "Rintu",
                "primary_email": "rintu@example.com",
                "emails": [{"value": "rintu@example.com"}],
            }]
        })

    def get_contact(args):
        return _env_ok({
            "resource_name": "people/3",
            "display_name": "Rintu",
            "primary_email": "rintu@example.com",
            "emails": [{"value": "rintu@example.com"}],
        })

    _mock_tools(monkeypatch, {"search_contacts": search_contacts, "list_contacts": lambda a: _env_ok({}), "get_contact": get_contact})

    def fake_generate(req):
        captured["payload"] = req.messages[1].content
        assert '"field_status": "missing"' in captured["payload"]
        return _LLMResponse("Rintu does not have a phone number saved.")

    monkeypatch.setattr(contacts_read.llm_router, "generate", fake_generate)
    result = generate_contacts_read_response(
        user_id="user-1", connection_id="conn-1", message="what is Rintu's phone number?"
    )
    assert "phone" in result["assistant_response"].casefold() or "number" in result["assistant_response"].casefold()


def test_contact_without_requested_email(monkeypatch):
    captured = {}

    def search_contacts(args):
        return _env_ok({
            "contacts": [{
                "resource_name": "people/4",
                "display_name": "Rahul",
                "primary_phone": "123",
                "phones": [{"value": "123"}],
            }]
        })

    def get_contact(args):
        return _env_ok({
            "resource_name": "people/4",
            "display_name": "Rahul",
            "primary_phone": "123",
            "phones": [{"value": "123"}],
        })

    _mock_tools(monkeypatch, {"search_contacts": search_contacts, "list_contacts": lambda a: _env_ok({}), "get_contact": get_contact})

    def fake_generate(req):
        captured["payload"] = req.messages[1].content
        assert '"field_status": "missing"' in captured["payload"]
        return _LLMResponse("Rahul's email address is not available.")

    monkeypatch.setattr(contacts_read.llm_router, "generate", fake_generate)
    result = generate_contacts_read_response(
        user_id="user-1", connection_id="conn-1", message="find Rahul's email"
    )
    assert "email" in result["assistant_response"].casefold()


def test_no_matching_contact(monkeypatch):
    captured = {}

    def search_contacts(args):
        return _env_ok({"contacts": []})

    _mock_tools(monkeypatch, {"search_contacts": search_contacts, "list_contacts": lambda a: _env_ok({}), "get_contact": lambda a: _env_ok({})})

    def fake_generate(req):
        captured["payload"] = req.messages[1].content
        assert '"status": "no_match"' in captured["payload"]
        return _LLMResponse("I couldn't find a contact named Rintu.")

    monkeypatch.setattr(contacts_read.llm_router, "generate", fake_generate)
    result = generate_contacts_read_response(
        user_id="user-1", connection_id="conn-1", message="show me the contact no of rintu"
    )
    assert "find" in result["assistant_response"].casefold() or "couldn" in result["assistant_response"].casefold()


def test_multiple_matching_contacts(monkeypatch):
    captured = {}

    def search_contacts(args):
        return _env_ok({
            "contacts": [
                {"resource_name": "people/1", "display_name": "Rintu A", "primary_phone": "111"},
                {"resource_name": "people/2", "display_name": "Rintu B", "primary_phone": "222"},
            ]
        })

    _mock_tools(monkeypatch, {"search_contacts": search_contacts, "list_contacts": lambda a: _env_ok({}), "get_contact": lambda a: _env_ok({})})

    def fake_generate(req):
        captured["payload"] = req.messages[1].content
        assert '"status": "multiple_matches"' in captured["payload"]
        assert "Rintu A" in captured["payload"] and "Rintu B" in captured["payload"]
        return _LLMResponse("I found multiple contacts named Rintu. Which one do you mean: Rintu A or Rintu B?")

    monkeypatch.setattr(contacts_read.llm_router, "generate", fake_generate)
    result = generate_contacts_read_response(
        user_id="user-1", connection_id="conn-1", message="what is Rintu's phone number?"
    )
    assert "which" in result["assistant_response"].casefold() or "multiple" in result["assistant_response"].casefold()


def test_invalid_missing_resource_name_ignored(monkeypatch):
    def search_contacts(args):
        return _env_ok({
            "contacts": [
                {"display_name": "No Id", "primary_phone": "111"},
                {"resource_name": "", "display_name": "Empty Id", "primary_phone": "222"},
            ]
        })

    _mock_tools(monkeypatch, {"search_contacts": search_contacts, "list_contacts": lambda a: _env_ok({}), "get_contact": lambda a: _env_ok({})})
    monkeypatch.setattr(contacts_read.llm_router, "generate", lambda req: _LLMResponse("No verified match found."))
    result = generate_contacts_read_response(
        user_id="user-1", connection_id="conn-1", message="find Rintu's email"
    )
    # treated as no valid verified match -> grounded response generated
    assert result["assistant_response"]


def test_search_tool_error_returns_safe_response(monkeypatch):
    _mock_tools(monkeypatch, {"search_contacts": lambda a: _env_err("Google API down"), "list_contacts": lambda a: _env_ok({}), "get_contact": lambda a: _env_ok({})})

    called = {"llm": 0}

    def fake_generate(req):
        called["llm"] += 1
        return _LLMResponse("should not be used")

    monkeypatch.setattr(contacts_read.llm_router, "generate", fake_generate)
    result = generate_contacts_read_response(
        user_id="user-1", connection_id="conn-1", message="find Rintu's email"
    )
    assert "couldn" in result["assistant_response"].casefold() or "try again" in result["assistant_response"].casefold()
    assert called["llm"] == 0  # error path does not fabricate via LLM


def test_llm_failure_produces_safe_grounded_fallback(monkeypatch):
    def search_contacts(args):
        return _env_ok({
            "contacts": [{
                "resource_name": "people/1",
                "display_name": "Rintu",
                "primary_phone": "+8801700000000",
            }]
        })

    _mock_tools(monkeypatch, {"search_contacts": search_contacts, "list_contacts": lambda a: _env_ok({}), "get_contact": lambda a: _env_ok({})})

    def boom(req):
        raise RuntimeError("llm down")

    monkeypatch.setattr(contacts_read.llm_router, "generate", boom)
    result = generate_contacts_read_response(
        user_id="user-1", connection_id="conn-1", message="show me the contact no of rintu"
    )
    assert result["assistant_response"]
    assert result["provider"] == ""
    assert result["model"] == ""
    # must not invent a different number; fallback is safe/non-fabricating
    assert "999" not in result["assistant_response"]


def test_list_contacts_uses_verified_outcome_and_llm(monkeypatch):
    def list_contacts(args):
        return _env_ok({
            "contacts": [
                {"resource_name": "people/1", "display_name": "A"},
                {"resource_name": "people/2", "display_name": "B"},
            ],
            "totalItems": 2,
        })

    _mock_tools(monkeypatch, {"list_contacts": list_contacts, "search_contacts": lambda a: _env_ok({}), "get_contact": lambda a: _env_ok({})})
    monkeypatch.setattr(contacts_read.llm_router, "generate", lambda req: _LLMResponse("You have 2 contacts: A and B."))
    result = generate_contacts_read_response(user_id="u", connection_id="c", message="show me all contacts")
    assert "2" in result["assistant_response"] or "A" in result["assistant_response"]


def test_count_contacts_uses_verified_outcome(monkeypatch):
    def list_contacts(args):
        return _env_ok({"contacts": [{"resource_name": "people/1"}] * 5, "totalItems": 42})

    _mock_tools(monkeypatch, {"list_contacts": list_contacts, "search_contacts": lambda a: _env_ok({}), "get_contact": lambda a: _env_ok({})})
    monkeypatch.setattr(contacts_read.llm_router, "generate", lambda req: _LLMResponse("You have 999 contacts."))
    result = generate_contacts_read_response(user_id="u", connection_id="c", message="how many contacts do I have?")
    assert "42" in result["assistant_response"]
    assert "999" not in result["assistant_response"]


# --- Node/graph integration & regressions ---

def test_contacts_agent_node_read_uses_grounded_workflow(monkeypatch):
    state, runtime = _state_and_runtime("show me the contact no of rintu")
    called = {}

    def fake_generate(**kwargs):
        called.update(kwargs)
        return {"assistant_response": "Rintu: +8801...", "provider": "p", "model": "m", "messages": []}

    monkeypatch.setattr(
        "app.workflows.contacts_agent_node.generate_contacts_read_response",
        fake_generate,
    )
    result = contacts_agent_node(state, runtime)
    assert called["user_id"] == "user-1"
    assert called["connection_id"] == "conn-1"
    assert "rintu" in called["message"].casefold()
    assert result["assistant_response"].startswith("Rintu")


def test_contacts_agent_node_mutation_unavailable_in_read_only_mode(monkeypatch):
    state, runtime = _state_and_runtime("create a contact named Ada")
    result = contacts_agent_node(state, runtime)
    assert "contacts_action_proposal" not in result
    assert "creating, updating, and deleting contacts is not available" in result["assistant_response"].casefold()
    assert "haven’t made any changes" in result["assistant_response"].casefold() or "haven't made any changes" in result["assistant_response"].casefold()


def test_contacts_read_requires_read_permission(monkeypatch):
    state, runtime = _state_and_runtime("show me all contacts", connection_id=None, write_connection_id="w1")
    result = contacts_agent_node(state, runtime)
    assert "read permission" in result["assistant_response"].casefold() or "grant" in result["assistant_response"].casefold()


def test_contacts_selection_required_safe(monkeypatch):
    state, runtime = _state_and_runtime("show me all contacts", selection_required=True)
    runtime.context["contacts_candidate_emails"] = ["a@example.com", "b@example.com"]
    result = contacts_agent_node(state, runtime)
    assert "choose which account" in result["assistant_response"].casefold()


def test_contacts_agent_node_not_connected(monkeypatch):
    state, runtime = _state_and_runtime("show me all contacts", connection_id=None, write_connection_id=None)
    result = contacts_agent_node(state, runtime)
    assert "not connected" in result["assistant_response"].casefold()


def test_specific_contact_request_never_falls_back_to_list(monkeypatch):
    # Ensure search tool is used, not list, for specific phone lookup.
    calls = {"list": 0, "search": 0}

    def list_contacts(args):
        calls["list"] += 1
        return _env_ok({"contacts": [{"resource_name": "people/x", "display_name": "X"}], "totalItems": 100})

    def search_contacts(args):
        calls["search"] += 1
        return _env_ok({"contacts": [{"resource_name": "people/1", "display_name": "Rintu", "primary_phone": "123"}]})

    _mock_tools(monkeypatch, {"list_contacts": list_contacts, "search_contacts": search_contacts, "get_contact": lambda a: _env_ok({})})
    monkeypatch.setattr(contacts_read.llm_router, "generate", lambda req: _LLMResponse("Rintu's number is 123."))
    result = generate_contacts_read_response(user_id="u", connection_id="c", message="show me the contact no of rintu")
    assert calls["search"] == 1
    assert calls["list"] == 0
    assert "100" not in result["assistant_response"]  # no list/count fallback leakage


def test_clean_target_extraction_from_conversational_query(monkeypatch):
    calls = {}

    def search_contacts(args):
        calls["query"] = args["query"]
        return _env_ok({"contacts": []})

    _mock_tools(monkeypatch, {"search_contacts": search_contacts, "list_contacts": lambda a: _env_ok({}), "get_contact": lambda a: _env_ok({})})
    monkeypatch.setattr(contacts_read.llm_router, "generate", lambda req: _LLMResponse("Not found."))
    generate_contacts_read_response(user_id="u", connection_id="c", message="show me the contact no of rintu")
    assert calls["query"].strip().casefold() == "rintu"
