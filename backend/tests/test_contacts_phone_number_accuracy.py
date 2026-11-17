import json
from types import SimpleNamespace

import pytest

from app.workflows import contacts_read
from app.workflows.contacts_agent_node import contacts_agent_node


SUBHRO = {
    "resource_name": "people/subhro-1",
    "display_name": "Subhro",
    "given_name": "Subhro",
    "primary_phone": "+91 92309 85025",
    "phones": [{"value": "+91 92309 85025", "type": "mobile", "primary": True}],
    "emails": [],
}
SUBHRO_EMOJI = {
    "resource_name": "people/subhro-2",
    "display_name": "Subhro ❤️ 🐻",
    "given_name": "Subhro",
    "primary_phone": "+91 70019 06952",
    "phones": [{"value": "+91 70019 06952", "type": "mobile", "primary": True}],
    "emails": [],
}
HALLUCINATED = "+91 98765 43210"


class _FakeTool:
    def __init__(self, name, handler):
        self.name = name
        self._handler = handler

    async def ainvoke(self, args):
        return self._handler(args)


class _LLMResponse:
    def __init__(self, content="unexpected"):
        self.content = content
        self.provider = "mock"
        self.model = "mock"

    def completion_metadata(self):
        return {"status": "complete"}


def _env_ok(data):
    return json.dumps({"ok": True, "data": data})


def _mock_tools(monkeypatch, handlers):
    tools = [_FakeTool(name, fn) for name, fn in handlers.items()]
    monkeypatch.setattr(contacts_read, "create_contacts_tools", lambda user_id, connection_id: tools)
    monkeypatch.setattr(contacts_read, "extract_contacts_read_intent_llm", lambda message: None)
    return tools


def _state_and_runtime(message, *, user_id="user-1", connection_id="conn-1", conversation_id="conv-1"):
    state = {
        "user_id": user_id,
        "conversation_id": conversation_id,
        "user_message": message,
        "messages": [],
    }
    runtime = SimpleNamespace(
        context={
            "user_id": user_id,
            "contacts_connection_id": connection_id,
            "contacts_write_connection_id": connection_id,
            "contacts_enabled": True,
            "contacts_selection_required": False,
            "contacts_candidate_emails": [],
            "contacts_read_scope_missing": False,
            "contacts_write_scope_missing": False,
            "contacts_google_connection_available": True,
            "google_connection_available": True,
        }
    )
    return state, runtime


def test_exact_contact_mapping_all_numbers_associated(monkeypatch):
    """Test A: exact mapping for all phone numbers associated with matching name."""
    def search_contacts(args):
        return _env_ok({"contacts": [SUBHRO, SUBHRO_EMOJI]})

    _mock_tools(monkeypatch, {
        "search_contacts": search_contacts,
        "list_contacts": lambda a: _env_ok({}),
        "get_contact": lambda a: _env_ok({}),
    })
    # If LLM were consulted, it must not affect output.
    monkeypatch.setattr(contacts_read.llm_router, "generate", lambda req: _LLMResponse(
        f"Subhro: {SUBHRO['primary_phone']}, Subhro ❤️ 🐻: {HALLUCINATED}"
    ))

    result = contacts_read.generate_contacts_read_response(
        user_id="user-1",
        connection_id="conn-1",
        message="give me all phone numbers associated with Subhro",
        conversation_id="conv-1",
    )
    text = result["assistant_response"]
    assert SUBHRO["primary_phone"] in text
    assert SUBHRO_EMOJI["primary_phone"] in text
    # Each number must be associated with the correct contact name in output.
    # Split by contact lines to ensure mapping (simple containment with name context).
    assert "Subhro: +91 92309 85025" in text or "- Subhro: +91 92309 85025" in text
    assert "Subhro ❤️ 🐻: +91 70019 06952" in text or "- Subhro ❤️ 🐻: +91 70019 06952" in text
    assert HALLUCINATED not in text
    assert result.get("pending_contacts_clarification") is None
    provenance = result.get("contacts_provenance") or {}
    assert provenance.get("verified_source") is True


def test_no_hallucinated_numbers_and_missing_phone(monkeypatch):
    """Test B: never return hallucinated number; say no phone stored when missing."""
    subhro_no_phone = {
        "resource_name": "people/nophone-1",
        "display_name": "Subhro (work)",
        "given_name": "Subhro",
        "primary_phone": None,
        "phones": [],
        "emails": [],
    }

    def search_contacts(args):
        return _env_ok({"contacts": [SUBHRO, subhro_no_phone]})

    _mock_tools(monkeypatch, {
        "search_contacts": search_contacts,
        "list_contacts": lambda a: _env_ok({}),
        "get_contact": lambda a: _env_ok({}),
    })
    monkeypatch.setattr(contacts_read.llm_router, "generate", lambda req: _LLMResponse(HALLUCINATED))

    result = contacts_read.generate_contacts_read_response(
        user_id="user-1",
        connection_id="conn-1",
        message="show all phone numbers for Subhro",
        conversation_id="conv-1",
    )
    text = result["assistant_response"]
    assert SUBHRO["primary_phone"] in text
    assert HALLUCINATED not in text
    assert "No phone number available" in text or "not have a phone number" in text.casefold() or "no phone" in text.casefold()


def test_similar_names_and_emoji_preserve_identity(monkeypatch):
    """Test C: similar names/emojis keep correct contact-to-number mapping."""
    contacts = [
        {
            "resource_name": "people/a",
            "display_name": "Alex",
            "phones": [{"value": "+1 111-111-1111", "primary": True}],
            "primary_phone": "+1 111-111-1111",
        },
        {
            "resource_name": "people/b",
            "display_name": "Alex 🙂",
            "phones": [{"value": "+1 222-222-2222", "primary": True}],
            "primary_phone": "+1 222-222-2222",
        },
        {
            "resource_name": "people/c",
            "display_name": "Alex 🚀",
            "phones": [{"value": "+1 333-333-3333", "primary": True}],
            "primary_phone": "+1 333-333-3333",
        },
    ]

    def search_contacts(args):
        return _env_ok({"contacts": contacts})

    _mock_tools(monkeypatch, {
        "search_contacts": search_contacts,
        "list_contacts": lambda a: _env_ok({}),
        "get_contact": lambda a: _env_ok({}),
    })
    result = contacts_read.generate_contacts_read_response(
        user_id="user-1",
        connection_id="conn-1",
        message="list all phone numbers associated with Alex",
        conversation_id="conv-1",
    )
    text = result["assistant_response"]
    assert "- Alex: +1 111-111-1111" in text
    assert "- Alex 🙂: +1 222-222-2222" in text
    assert "- Alex 🚀: +1 333-333-3333" in text
    assert HALLUCINATED not in text


def test_stale_context_does_not_override_fresh_contacts_data(monkeypatch):
    """Test D: stale/conflicting context must not override fresh authoritative tool results."""
    calls = {"search": 0}

    def search_contacts(args):
        calls["search"] += 1
        return _env_ok({"contacts": [SUBHRO, SUBHRO_EMOJI]})

    _mock_tools(monkeypatch, {
        "search_contacts": search_contacts,
        "list_contacts": lambda a: _env_ok({}),
        "get_contact": lambda a: _env_ok({}),
    })
    monkeypatch.setattr(contacts_read.llm_router, "generate", lambda req: _LLMResponse(HALLUCINATED))

    state, runtime = _state_and_runtime(
        "give me all phone numbers associated with Subhro",
        user_id="user-1",
        connection_id="conn-1",
    )
    # Simulate stale conversational context that could mislead a non-grounded responder.
    state["messages"] = [
        {"role": "assistant", "content": f"Subhro ❤️ 🐻: {HALLUCINATED}"},
        {"role": "user", "content": "remember that number"},
    ]
    # Even if pending clarification were unrelated/stale, read path must use fresh API data.
    state["pending_contacts_clarification"] = None

    result = contacts_agent_node(state, runtime)
    text = result["assistant_response"]
    assert calls["search"] >= 1
    assert SUBHRO_EMOJI["primary_phone"] in text
    assert HALLUCINATED not in text
    assert SUBHRO["primary_phone"] in text


def test_multi_user_isolation_uses_authenticated_user_and_connection(monkeypatch):
    """Test E: contacts lookup must be scoped to authenticated user/selected connection only."""
    seen = {}

    def fake_tools(user_id, connection_id):
        seen["user_id"] = str(user_id)
        seen["connection_id"] = str(connection_id)

        def search_contacts(args):
            return _env_ok({"contacts": [SUBHRO]})

        st = _FakeTool("search_contacts", search_contacts)
        gt = _FakeTool("get_contact", lambda a: _env_ok({}))
        lt = _FakeTool("list_contacts", lambda a: _env_ok({}))
        return [lt, st, gt]

    monkeypatch.setattr(contacts_read, "create_contacts_tools", fake_tools)
    monkeypatch.setattr(contacts_read, "extract_contacts_read_intent_llm", lambda message: None)

    state, runtime = _state_and_runtime(
        "show me Subhro's number",
        user_id="user-A",
        connection_id="conn-A",
    )
    result = contacts_agent_node(state, runtime)
    assert seen["user_id"] == "user-A"
    assert seen["connection_id"] == "conn-A"
    assert "user-B" not in json.dumps(result, default=str)
    assert "conn-B" not in json.dumps(result, default=str)
    assert HALLUCINATED not in result["assistant_response"]
