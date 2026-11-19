import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.schemas.contacts_read import ContactsReadExtraction
from app.workflows import contacts_read


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


def _mock_tools(monkeypatch, handlers, mock_extraction=True):
    tools = [_FakeTool(name, fn) for name, fn in handlers.items()]
    monkeypatch.setattr(contacts_read, "create_contacts_tools", lambda user_id, connection_id: tools)
    if mock_extraction:
        monkeypatch.setattr(contacts_read, "extract_contacts_read_intent_llm", lambda message: None)
    return tools


class _LLMResponse:
    def __init__(self, content):
        self.content = content
        self.provider = "test-provider"
        self.model = "test-model"

    def completion_metadata(self):
        return {"status": "complete", "finish_reason": "stop"}


def _extraction_json(**overrides):
    base = {
        "operation": "get_contact_phone",
        "target": {"name": "Rintu", "email": None, "phone": None},
        "requested_attribute": "phone",
        "confidence": "high",
        "ambiguity_reason": None,
        "requires_clarification": False,
    }
    base.update(overrides)
    return json.dumps(base, ensure_ascii=False)


# --- Schema / extraction validation ---

def test_schema_rejects_unexpected_identifiers():
    with pytest.raises(ValidationError):
        ContactsReadExtraction.model_validate({
            "operation": "get_contact_phone",
            "target": {"name": "Rintu", "email": None, "phone": None, "resource_name": "people/1"},
            "requested_attribute": "phone",
            "confidence": "high",
            "ambiguity_reason": None,
            "requires_clarification": False,
        })
    with pytest.raises(ValidationError):
        ContactsReadExtraction.model_validate({
            "operation": "get_contact_phone",
            "target": {"name": "Rintu", "email": None, "phone": None},
            "requested_attribute": "phone",
            "confidence": "high",
            "ambiguity_reason": None,
            "requires_clarification": False,
            "contact_id": "x",
        })


def test_llm_extraction_valid_english_phone(monkeypatch):
    monkeypatch.setattr(contacts_read.llm_router, "generate", lambda req: _LLMResponse(_extraction_json()))
    ext = contacts_read.extract_contacts_read_intent_llm("Show me Rintu's phone number.")
    assert ext is not None
    assert ext.operation == "get_contact_phone"
    assert ext.target.name == "Rintu"
    assert ext.requested_attribute == "phone"


@pytest.mark.parametrize("message,content", [
    ("রিন্টুর ফোন নম্বর দেখাও।", _extraction_json(operation="get_contact_phone", target={"name": "রিন্টু", "email": None, "phone": None}, requested_attribute="phone")),
    ("রাহুলের ইমেইল দাও।", _extraction_json(operation="get_contact_email", target={"name": "রাহুল", "email": None, "phone": None}, requested_attribute="email")),
    ("রিন্টুর সব contact details দেখাও।", _extraction_json(operation="get_contact_details", target={"name": "রিন্টু", "email": None, "phone": None}, requested_attribute="details")),
    ("Rintu er phone number ta dao.", _extraction_json(operation="get_contact_phone", target={"name": "Rintu", "email": None, "phone": None}, requested_attribute="phone")),
    ("Rahul er email address ki?", _extraction_json(operation="get_contact_email", target={"name": "Rahul", "email": None, "phone": None}, requested_attribute="email")),
    ("Rahul ka mobile number batao.", _extraction_json(operation="get_contact_phone", target={"name": "Rahul", "email": None, "phone": None}, requested_attribute="phone")),
])
def test_llm_extraction_multilingual_examples(monkeypatch, message, content):
    monkeypatch.setattr(contacts_read.llm_router, "generate", lambda req: _LLMResponse(content))
    ext = contacts_read.extract_contacts_read_intent_llm(message)
    assert ext is not None
    assert ext.target.name


def test_llm_extraction_list_vs_count(monkeypatch):
    monkeypatch.setattr(contacts_read.llm_router, "generate", lambda req: _LLMResponse(_extraction_json(operation="list_contacts", target={"name": None, "email": None, "phone": None}, requested_attribute="none")))
    ext = contacts_read.extract_contacts_read_intent_llm("show all contacts")
    assert ext is not None and ext.operation == "list_contacts"
    monkeypatch.setattr(contacts_read.llm_router, "generate", lambda req: _LLMResponse(_extraction_json(operation="count_contacts", target={"name": None, "email": None, "phone": None}, requested_attribute="none")))
    ext = contacts_read.extract_contacts_read_intent_llm("how many contacts?")
    assert ext is not None and ext.operation == "count_contacts"


def test_llm_extraction_malformed_json_returns_none(monkeypatch):
    monkeypatch.setattr(contacts_read.llm_router, "generate", lambda req: _LLMResponse("not json {{{"))
    assert contacts_read.extract_contacts_read_intent_llm("find rintu") is None


def test_llm_extraction_invalid_schema_or_extra_ids_returns_none(monkeypatch):
    monkeypatch.setattr(contacts_read.llm_router, "generate", lambda req: _LLMResponse(json.dumps({
        "operation": "get_contact_phone",
        "target": {"name": "Rintu", "email": None, "phone": None},
        "requested_attribute": "phone",
        "confidence": "high",
        "ambiguity_reason": None,
        "requires_clarification": False,
        "resource_name": "people/123",
    })))
    assert contacts_read.extract_contacts_read_intent_llm("find rintu") is None


def test_llm_extraction_provider_failure_returns_none(monkeypatch):
    def boom(req):
        raise RuntimeError("provider down")
    monkeypatch.setattr(contacts_read.llm_router, "generate", boom)
    assert contacts_read.extract_contacts_read_intent_llm("find rintu") is None


def test_low_confidence_or_missing_target_falls_back_or_clarifies(monkeypatch):
    # Low confidence + deterministic can resolve clearly -> fallback to deterministic
    monkeypatch.setattr(contacts_read.llm_router, "generate", lambda req: _LLMResponse(_extraction_json(confidence="low", target={"name": "rintu", "email": None, "phone": None})))
    intent, ext, source = contacts_read.resolve_contacts_read_intent("show me the contact no of rintu")
    assert intent.intent == "get_contact_phone"
    assert intent.target_query and "rintu" in intent.target_query.casefold()
    # Missing target ambiguous
    monkeypatch.setattr(contacts_read.llm_router, "generate", lambda req: _LLMResponse(_extraction_json(operation="get_contact_phone", target={"name": None, "email": None, "phone": None}, requires_clarification=True, ambiguity_reason="missing_target", confidence="medium")))
    intent, ext, source = contacts_read.resolve_contacts_read_intent("show me the contact number")
    assert intent.ambiguous or intent.intent == "unknown"


# --- Resolver / matching / results ---

def test_unique_partial_match_requires_confirmation(monkeypatch):
    calls = {"search": 0}
    def search(args):
        calls["search"] += 1
        return _env_ok({"contacts": [{"resource_name": "people/1", "display_name": "Rintu Das", "primary_phone": "111"}]})
    _mock_tools(monkeypatch, {"search_contacts": search, "list_contacts": lambda a: _env_ok({}), "get_contact": lambda a: _env_ok({})})
    monkeypatch.setattr(contacts_read.llm_router, "generate", lambda req: _LLMResponse("Rintu Das: 111"))
    result = contacts_read.generate_contacts_read_response(user_id="u", connection_id="c", message="find Rintu's phone number", conversation_id="conv-1")
    assert calls["search"] >= 1
    # No exact match + partial match => must ask for confirmation (no silent fuzzy selection), no phone exposed
    assert result.get("pending_contacts_clarification") is not None
    assert "Rintu Das" in result["assistant_response"]
    assert "111" not in result["assistant_response"]
    assert "confirm" in result["assistant_response"].casefold() or "which contact" in result["assistant_response"].casefold()


def test_multiple_matches_return_clarification_context(monkeypatch):
    def search(args):
        return _env_ok({"contacts": [
            {"resource_name": "people/1", "display_name": "Rintu A", "primary_phone": "111"},
            {"resource_name": "people/2", "display_name": "Rintu B", "primary_phone": "222"},
        ]})
    _mock_tools(monkeypatch, {"search_contacts": search, "list_contacts": lambda a: _env_ok({}), "get_contact": lambda a: _env_ok({})})
    monkeypatch.setattr(contacts_read.llm_router, "generate", lambda req: _LLMResponse("Which Rintu do you mean: Rintu A or Rintu B?"))
    result = contacts_read.generate_contacts_read_response(user_id="u", connection_id="c", message="find Rintu's phone number", conversation_id="conv-1")
    assert result.get("pending_contacts_clarification") is not None
    pending = result["pending_contacts_clarification"]
    assert pending["type"] == "contacts_read_clarification"
    assert pending["conversation_id"] == "conv-1"
    assert pending["user_id"] == "u"
    assert len(pending.get("candidates") or []) == 2


def test_pagination_bounded(monkeypatch):
    calls = {"n": 0}
    def search(args):
        calls["n"] += 1
        token = "tok2" if calls["n"] == 1 else None
        return _env_ok({
            "contacts": [{"resource_name": f"people/{calls['n']}", "display_name": f"Rintu {calls['n']}"}],
            "next_page_token": token,
        })
    _mock_tools(monkeypatch, {"search_contacts": search, "list_contacts": lambda a: _env_ok({}), "get_contact": lambda a: _env_ok({})})
    monkeypatch.setattr(contacts_read.llm_router, "generate", lambda req: _LLMResponse("Multiple matches found."))
    contacts_read.generate_contacts_read_response(user_id="u", connection_id="c", message="find Rintu", conversation_id="conv-1")
    assert calls["n"] <= 2


def test_invalid_resource_name_ignored(monkeypatch):
    def search(args):
        return _env_ok({"contacts": [
            {"resource_name": "invalid", "display_name": "Bad", "primary_phone": "000"},
            {"resource_name": "people/ok1", "display_name": "Rintu", "primary_phone": "111"},
        ]})
    _mock_tools(monkeypatch, {"search_contacts": search, "list_contacts": lambda a: _env_ok({}), "get_contact": lambda a: _env_ok({})})
    captured = {}
    def fake_llm(req):
        captured["payload"] = req.messages[1].content
        assert "people/ok1" in captured["payload"]
        assert "invalid" not in captured["payload"]
        return _LLMResponse("Rintu: 111")
    monkeypatch.setattr(contacts_read.llm_router, "generate", fake_llm)
    contacts_read.generate_contacts_read_response(user_id="u", connection_id="c", message="find Rintu phone")


# --- Clarification follow-up resolution ---

def _pending(candidates, user_id="u", conversation_id="conv-1", attr="phone", target="Rintu", expired=False):
    exp = datetime.now(timezone.utc) + (timedelta(seconds=-10) if expired else timedelta(minutes=10))
    return {
        "version": 1,
        "type": "contacts_read_clarification",
        "user_id": user_id,
        "conversation_id": conversation_id,
        "expires_at": exp.isoformat(),
        "target_query": target,
        "requested_attribute": attr,
        "candidates": candidates,
    }


def test_followup_selects_trusted_candidate(monkeypatch):
    candidates = [
        {"resource_name": "people/1", "display_name": "Rintu Das"},
        {"resource_name": "people/2", "display_name": "Rintu Roy"},
    ]
    pending = _pending(candidates)
    calls = {"get": 0}
    def get_contact(args):
        calls["get"] += 1
        assert args["resource_name"] == "people/2"
        return _env_ok({"resource_name": "people/2", "display_name": "Rintu Roy", "primary_phone": "222"})
    _mock_tools(monkeypatch, {"get_contact": get_contact, "search_contacts": lambda a: _env_ok({}), "list_contacts": lambda a: _env_ok({})})
    monkeypatch.setattr(contacts_read.llm_router, "generate", lambda req: _LLMResponse("FABRICATED 555"))
    result = contacts_read.generate_contacts_read_response(
        user_id="u", connection_id="c", message="Rintu Roy", conversation_id="conv-1", pending_clarification=pending,
    )
    assert calls["get"] == 1
    assert "222" in result["assistant_response"]
    assert "555" not in result["assistant_response"]


def test_numeric_clarification_selection_preserves_trusted_first_contact(monkeypatch):
    """Clarification reply '1' must resolve to the first trusted candidate deterministically
    (same logical selection), without re-asking or selecting another contact."""
    calls = {"get": 0, "search": 0, "list": 0}

    def get_contact(args):
        calls["get"] += 1
        assert args["resource_name"] == "people/1"
        return _env_ok({
            "resource_name": "people/1",
            "display_name": "Subbhro Das",
            "primary_phone": "111-111",
            "phones": ["111-111"],
        })

    _mock_tools(monkeypatch, {
        "get_contact": get_contact,
        "search_contacts": lambda a: (calls.__setitem__("search", calls["search"] + 1), _env_ok({"contacts": []}))[1],
        "list_contacts": lambda a: (calls.__setitem__("list", calls["list"] + 1), _env_ok({"contacts": []}))[1],
    })
    monkeypatch.setattr(
        contacts_read.llm_router,
        "generate",
        lambda req: (_ for _ in ()).throw(AssertionError("LLM should not be called for numeric clarification selection")),
    )

    candidates = [
        {"resource_name": "people/1", "display_name": "Subbhro Das", "phone_hint": "**11", "has_phone": True},
        {"resource_name": "people/2", "display_name": "Subbhro Roy", "phone_hint": "**22", "has_phone": True},
    ]
    pending = _pending(candidates, target="Subbhro")
    result = contacts_read.generate_contacts_read_response(
        user_id="u",
        connection_id="c",
        message="1",
        conversation_id="conv-1",
        pending_clarification=pending,
    )

    assert calls["get"] == 1
    assert calls["search"] == 0
    assert calls["list"] == 0
    assert "111-111" in result["assistant_response"]
    assert "222" not in result["assistant_response"]
    assert result.get("pending_contacts_clarification") is None


def test_unrelated_candidate_selection_rejected(monkeypatch):
    candidates = [
        {"resource_name": "people/1", "display_name": "Rintu Das", "phone_hint": "**11", "has_phone": True},
        {"resource_name": "people/2", "display_name": "Rintu Roy", "phone_hint": "**22", "has_phone": True},
    ]
    pending = _pending(candidates)
    def get_contact(args):
        raise AssertionError("should not fetch when selection unresolved")
    _mock_tools(monkeypatch, {"get_contact": get_contact, "search_contacts": lambda a: _env_ok({}), "list_contacts": lambda a: _env_ok({})})
    monkeypatch.setattr(contacts_read.llm_router, "generate", lambda req: _LLMResponse("Please clarify which contact you mean."))
    result = contacts_read.generate_contacts_read_response(
        user_id="u", connection_id="c", message="someone else entirely", conversation_id="conv-1", pending_clarification=pending,
    )
    # unresolved -> clarification remains (refreshed) or asks again
    assert result.get("pending_contacts_clarification") is not None


def test_cross_user_or_expired_clarification_rejected(monkeypatch):
    candidates = [{"resource_name": "people/1", "display_name": "Rintu Das", "phone_hint": "**11", "has_phone": True}]
    # cross-user
    pending = _pending(candidates, user_id="other")
    _mock_tools(monkeypatch, {"search_contacts": lambda a: _env_ok({"contacts": []}), "list_contacts": lambda a: _env_ok({}), "get_contact": lambda a: _env_ok({})})
    monkeypatch.setattr(contacts_read.llm_router, "generate", lambda req: _LLMResponse("Not found."))
    # should not resolve via clarification; will proceed with normal extraction/search flow
    result = contacts_read.generate_contacts_read_response(
        user_id="u", connection_id="c", message="Rintu Das", conversation_id="conv-1", pending_clarification=pending,
    )
    assert result.get("pending_contacts_clarification") is None or result["pending_contacts_clarification"]["user_id"] == "u" or result["pending_contacts_clarification"] is None
    # expired
    pending2 = _pending(candidates, expired=True)
    result2 = contacts_read.generate_contacts_read_response(
        user_id="u", connection_id="c", message="Rintu Das", conversation_id="conv-1", pending_clarification=pending2,
    )
    assert result2  # handled without trusting expired context


def test_clarification_response_excludes_contact_field_values(monkeypatch):
    def search(args):
        return _env_ok({"contacts": [
            {"resource_name": "people/1", "display_name": "Rintu A", "primary_phone": "111"},
            {"resource_name": "people/2", "display_name": "Rintu B", "primary_phone": "222"},
        ]})
    _mock_tools(monkeypatch, {"search_contacts": search, "list_contacts": lambda a: _env_ok({}), "get_contact": lambda a: _env_ok({})})
    calls = {"llm": 0}
    def fake_llm(req):
        calls["llm"] += 1
        return _LLMResponse("Rintu A is 111, Rintu B is 222")
    monkeypatch.setattr(contacts_read.llm_router, "generate", fake_llm)
    result = contacts_read.generate_contacts_read_response(user_id="u", connection_id="c", message="find Rintu phone", conversation_id="conv-1")
    assert calls["llm"] == 0
    assert "111" not in result["assistant_response"]
    assert "222" not in result["assistant_response"]
    assert "Rintu A" in result["assistant_response"] and "Rintu B" in result["assistant_response"]
import pytest

# --- Phase 3C.2 pagination/resolution hardening ---

def test_search_match_on_second_page_receives_correct_page_token(monkeypatch):
    calls = []
    def search(args):
        calls.append(dict(args))
        if len(calls) == 1:
            return _env_ok({"contacts": [{"resource_name": "people/1", "display_name": "Someone Else"}], "next_page_token": "tok-2"})
        return _env_ok({"contacts": [{"resource_name": "people/2", "display_name": "Rintu", "primary_phone": "999", "phones": [{"value": "999"}]}], "next_page_token": None})
    def get_contact(args):
        if args.get("resource_name") == "people/2":
            return _env_ok({"resource_name": "people/2", "display_name": "Rintu", "primary_phone": "999", "phones": [{"value": "999"}]})
        return _env_ok({"resource_name": args.get("resource_name"), "display_name": "Someone"})
    _mock_tools(monkeypatch, {"search_contacts": search, "list_contacts": lambda a: _env_ok({}), "get_contact": get_contact})
    monkeypatch.setattr(contacts_read.llm_router, "generate", lambda req: _LLMResponse("FABRICATED 555"))
    result = contacts_read.generate_contacts_read_response(user_id="u", connection_id="c", message="find Rintu's phone number")
    assert calls[0].get("page_token") in (None, "")
    assert calls[1].get("page_token") == "tok-2"
    assert "999" in result["assistant_response"]
    assert "555" not in result["assistant_response"]


def test_search_incomplete_when_match_beyond_configured_limit(monkeypatch):
    calls = {"n": 0}
    def search(args):
        calls["n"] += 1
        # always return non-matching contacts and another page token to simulate match beyond limit
        return _env_ok({
            "contacts": [{"resource_name": f"people/{calls['n']}", "display_name": f"Other {calls['n']}"}],
            "next_page_token": f"tok-{calls['n']+1}",
        })
    _mock_tools(monkeypatch, {"search_contacts": search, "list_contacts": lambda a: _env_ok({}), "get_contact": lambda a: _env_ok({})})
    monkeypatch.setattr(contacts_read.llm_router, "generate", lambda req: _LLMResponse("Search was limited, please be more specific."))
    result = contacts_read.generate_contacts_read_response(user_id="u", connection_id="c", message="find Rintu's phone number")
    assert calls["n"] <= contacts_read.CONTACTS_SEARCH_MAX_PAGES
    assert "limited" in result["assistant_response"].casefold() or "specific" in result["assistant_response"].casefold()


def test_search_repeated_page_token_stops_safely(monkeypatch):
    calls = {"n": 0}
    def search(args):
        calls["n"] += 1
        return _env_ok({
            "contacts": [{"resource_name": f"people/{calls['n']}", "display_name": f"Other {calls['n']}"}],
            "next_page_token": "same-token",
        })
    _mock_tools(monkeypatch, {"search_contacts": search, "list_contacts": lambda a: _env_ok({}), "get_contact": lambda a: _env_ok({})})
    monkeypatch.setattr(contacts_read.llm_router, "generate", lambda req: _LLMResponse("Search was limited, please be more specific."))
    result = contacts_read.generate_contacts_read_response(user_id="u", connection_id="c", message="find contact named Rintu")
    assert calls["n"] == 2  # first page, second page uses token, third would repeat -> stop before 3rd fetch
    assert "limited" in result["assistant_response"].casefold() or "specific" in result["assistant_response"].casefold() or "couldn" in result["assistant_response"].casefold()


def test_duplicate_contacts_across_pages_do_not_create_false_multiple(monkeypatch):
    calls = {"n": 0}
    def search(args):
        calls["n"] += 1
        if calls["n"] == 1:
            return _env_ok({
                "contacts": [{"resource_name": "people/1", "display_name": "Rintu Das", "primary_phone": "111"}],
                "next_page_token": "tok-2",
            })
        return _env_ok({
            "contacts": [{"resource_name": "people/1", "display_name": "Rintu Das", "primary_phone": "111"}],
            "next_page_token": None,
        })
    _mock_tools(monkeypatch, {"search_contacts": search, "list_contacts": lambda a: _env_ok({}), "get_contact": lambda a: _env_ok({})})
    monkeypatch.setattr(contacts_read.llm_router, "generate", lambda req: _LLMResponse("Rintu Das: 111"))
    result = contacts_read.generate_contacts_read_response(user_id="u", connection_id="c", message="find Rintu's phone number", conversation_id="conv-1")
    # Dedupe prevents false multiple distinct contacts; single partial still requires confirmation (no silent fuzzy selection)
    assert result.get("pending_contacts_clarification") is not None
    candidates = (result.get("pending_contacts_clarification") or {}).get("candidates") or []
    assert len(candidates) == 1
    assert "Rintu Das" in result["assistant_response"]
    assert "111" not in result["assistant_response"]


def test_multiple_matches_across_pages_trigger_clarification(monkeypatch):
    calls = {"n": 0}
    def search(args):
        calls["n"] += 1
        if calls["n"] == 1:
            return _env_ok({"contacts": [{"resource_name": "people/1", "display_name": "Rintu A", "primary_phone": "111"}], "next_page_token": "tok-2"})
        return _env_ok({"contacts": [{"resource_name": "people/2", "display_name": "Rintu B", "primary_phone": "222"}], "next_page_token": None})
    _mock_tools(monkeypatch, {"search_contacts": search, "list_contacts": lambda a: _env_ok({}), "get_contact": lambda a: _env_ok({})})
    monkeypatch.setattr(contacts_read.llm_router, "generate", lambda req: _LLMResponse("Which Rintu do you mean?"))
    result = contacts_read.generate_contacts_read_response(user_id="u", connection_id="c", message="find Rintu's phone number", conversation_id="conv-1")
    assert result.get("pending_contacts_clarification") is not None
    assert len(result["pending_contacts_clarification"].get("candidates") or []) >= 2


def test_search_api_error_during_pagination_returns_safe_result(monkeypatch):
    calls = {"n": 0}
    def search(args):
        calls["n"] += 1
        if calls["n"] == 1:
            return _env_ok({"contacts": [{"resource_name": "people/1", "display_name": "Rintu X"}], "next_page_token": "tok-2"})
        return _env_err("page 2 failed")
    _mock_tools(monkeypatch, {"search_contacts": search, "list_contacts": lambda a: _env_ok({}), "get_contact": lambda a: _env_ok({})})
    monkeypatch.setattr(contacts_read.llm_router, "generate", lambda req: _LLMResponse("FABRICATED 555"))
    result = contacts_read.generate_contacts_read_response(user_id="u", connection_id="c", message="find contact named Rintu", conversation_id="conv-1")
    # Partial collected candidate is handled safely (clarification/incomplete) and never fabricates contact numbers.
    assert "555" not in result["assistant_response"]
    assert "page 2 failed" not in result["assistant_response"]
    assert result["assistant_response"]


def test_pending_clarification_has_identity_markers(monkeypatch):
    def search(args):
        return _env_ok({"contacts": [
            {"resource_name": "people/1", "display_name": "Rintu A", "primary_phone": "111"},
            {"resource_name": "people/2", "display_name": "Rintu B", "primary_phone": "222"},
        ]})
    _mock_tools(monkeypatch, {"search_contacts": search, "list_contacts": lambda a: _env_ok({}), "get_contact": lambda a: _env_ok({})})
    monkeypatch.setattr(contacts_read.llm_router, "generate", lambda req: _LLMResponse("Which one?"))
    result = contacts_read.generate_contacts_read_response(user_id="u", connection_id="c", message="find contact named Rintu", conversation_id="conv-1")
    pending = result.get("pending_contacts_clarification")
    assert pending and pending.get("clarification_id") and pending.get("created_at")


def test_multiple_matches_all_fetches_authoritative_for_each(monkeypatch):
    """All candidates in multiple_matches_all must be fetched via get_contact and rendered from authoritative data."""
    subhro = {
        "resource_name": "people/s1",
        "display_name": "Subhro",
        "primary_phone": "+91 92309 85025",
        "phones": [{"value": "+91 92309 85025"}],
    }
    subhro_emoji = {
        "resource_name": "people/s2",
        "display_name": "Subhro ❤️ 🐻",
        "primary_phone": "+91 70019 06952",
        "phones": [{"value": "+91 70019 06952"}],
    }
    get_calls = []

    def search_contacts(args):
        return _env_ok({"contacts": [subhro, subhro_emoji]})

    def get_contact(args):
        get_calls.append(args.get("resource_name"))
        rn = args.get("resource_name")
        if rn == "people/s1":
            return _env_ok(subhro)
        if rn == "people/s2":
            return _env_ok(subhro_emoji)
        return _env_ok({})

    _mock_tools(monkeypatch, {
        "search_contacts": search_contacts,
        "list_contacts": lambda a: _env_ok({}),
        "get_contact": get_contact,
    })
    monkeypatch.setattr(contacts_read.llm_router, "generate", lambda req: _LLMResponse("nope"))

    result = contacts_read.generate_contacts_read_response(
        user_id="u1", connection_id="c1",
        message="give me all phone numbers associated with Subhro",
        conversation_id="conv1",
    )
    text = result.get("assistant_response") or ""
    assert "92309" in text
    assert "70019" in text
    assert set(get_calls) == {"people/s1", "people/s2"}


def test_multiple_matches_all_partial_failure_continues(monkeypatch):
    """If one get_contact fails, show successful ones and a note about the failed contact."""
    subhro = {
        "resource_name": "people/s1",
        "display_name": "Subhro",
        "primary_phone": "+91 92309 85025",
        "phones": [{"value": "+91 92309 85025"}],
    }
    subhro_emoji = {
        "resource_name": "people/s2",
        "display_name": "Subhro ❤️ 🐻",
        "primary_phone": "+91 70019 06952",
        "phones": [{"value": "+91 70019 06952"}],
    }

    def search_contacts(args):
        return _env_ok({"contacts": [subhro, subhro_emoji]})

    def get_contact(args):
        rn = args.get("resource_name")
        if rn == "people/s1":
            return _env_ok(subhro)
        if rn == "people/s2":
            return _env_err("boom")
        return _env_ok({})

    _mock_tools(monkeypatch, {
        "search_contacts": search_contacts,
        "list_contacts": lambda a: _env_ok({}),
        "get_contact": get_contact,
    })
    monkeypatch.setattr(contacts_read.llm_router, "generate", lambda req: _LLMResponse("nope"))

    result = contacts_read.generate_contacts_read_response(
        user_id="u1", connection_id="c1",
        message="give me all phone numbers associated with Subhro",
        conversation_id="conv1",
    )
    text = result.get("assistant_response") or ""
    assert "+91 92309 85025" in text
    assert "Could not retrieve details for" in text


def test_multiple_matches_all_all_fail_returns_safe_error(monkeypatch):
    """If no authoritative retrieval succeeds for any candidate, return a safe error."""
    subhro = {
        "resource_name": "people/s1",
        "display_name": "Subhro",
        "primary_phone": "+91 92309 85025",
        "phones": [{"value": "+91 92309 85025"}],
    }
    subhro_emoji = {
        "resource_name": "people/s2",
        "display_name": "Subhro ❤️ 🐻",
        "primary_phone": "+91 70019 06952",
        "phones": [{"value": "+91 70019 06952"}],
    }

    def search_contacts(args):
        return _env_ok({"contacts": [subhro, subhro_emoji]})

    def get_contact(args):
        return _env_err("boom")

    _mock_tools(monkeypatch, {
        "search_contacts": search_contacts,
        "list_contacts": lambda a: _env_ok({}),
        "get_contact": get_contact,
    })
    monkeypatch.setattr(contacts_read.llm_router, "generate", lambda req: _LLMResponse("nope"))

    result = contacts_read.generate_contacts_read_response(
        user_id="u1", connection_id="c1",
        message="give me all phone numbers associated with Subhro",
        conversation_id="conv1",
    )
    text = (result.get("assistant_response") or "").lower()
    assert "couldn't" in text or "could not" in text


import pytest

@pytest.mark.parametrize("reply,expected_rn,expected_phone", [
    ("1", "people/p1", "+911111111111"),
    ("2", "people/p2", "+912222222222"),
    ("2.", "people/p2", "+912222222222"),
    ("option 2", "people/p2", "+912222222222"),
    ("number 2", "people/p2", "+912222222222"),
    ("choice 2", "people/p2", "+912222222222"),
    ("select 2", "people/p2", "+912222222222"),
    ("second one", "people/p2", "+912222222222"),
    ("Mua Paromita Di", "people/p2", "+912222222222"),
])
def test_numeric_clarification_matrix_valid(monkeypatch, reply, expected_rn, expected_phone):
    """Valid clarification replies select the correct candidate with authoritative get_contact."""
    candidates = [
        {"resource_name": "people/p1", "display_name": "Paromita di💕💕", "primary_phone": "+911111111111", "phones": [{"value": "+911111111111"}]},
        {"resource_name": "people/p2", "display_name": "Mua Paromita Di", "primary_phone": "+912222222222", "phones": [{"value": "+912222222222"}]},
        {"resource_name": "people/p3", "display_name": "Mua Paromita Di_2", "primary_phone": "+913333333333", "phones": [{"value": "+913333333333"}]},
    ]
    pending = {
        "version": 1,
        "type": "contacts_read_clarification",
        "clarification_id": "c1",
        "created_at": "2026-01-01T00:00:00+00:00",
        "expires_at": "2099-01-01T00:00:00+00:00",
        "user_id": "user-1",
        "conversation_id": "conv-1",
        "target_query": "Paromita Di",
        "requested_attribute": "details",
        "candidates": candidates,
    }
    search_calls = []
    get_calls = []

    def search_contacts(args):
        search_calls.append(dict(args))
        return _env_ok({"contacts": candidates})

    def get_contact(args):
        get_calls.append(dict(args))
        rn = args.get("resource_name")
        for c in candidates:
            if c["resource_name"] == rn:
                return _env_ok(c)
        return _env_ok({})

    _mock_tools(monkeypatch, {
        "search_contacts": search_contacts,
        "list_contacts": lambda a: _env_ok({}),
        "get_contact": get_contact,
    })
    monkeypatch.setattr(contacts_read.llm_router, "generate", lambda req: _LLMResponse("nope"))

    result = contacts_read.generate_contacts_read_response(
        user_id="user-1", connection_id="conn-1",
        message=reply,
        conversation_id="conv-1",
        pending_clarification=pending,
    )
    assert len(get_calls) >= 1
    assert get_calls[-1]["resource_name"] == expected_rn
    assert expected_phone in (result.get("assistant_response") or "")
    assert result.get("pending_contacts_clarification") is None


def test_multiple_matches_all_wrong_resource_name_treated_as_failure(monkeypatch):
    """If get_contact returns a different resource_name, treat as fetch failure (no cross-contact data)."""
    subhro = {
        "resource_name": "people/s1",
        "display_name": "Subhro",
        "primary_phone": "+91 92309 85025",
        "phones": [{"value": "+91 92309 85025"}],
    }
    subhro_emoji = {
        "resource_name": "people/s2",
        "display_name": "Subhro ❤️ 🐻",
        "primary_phone": "+91 70019 06952",
        "phones": [{"value": "+91 70019 06952"}],
    }

    def search_contacts(args):
        return _env_ok({"contacts": [subhro, subhro_emoji]})

    def get_contact(args):
        rn = args.get("resource_name")
        if rn == "people/s1":
            # Wrong resource returned!
            return _env_ok({"resource_name": "people/WRONG", "display_name": "Wrong", "primary_phone": "+999", "phones": [{"value": "+999"}]})
        if rn == "people/s2":
            return _env_ok(subhro_emoji)
        return _env_ok({})

    _mock_tools(monkeypatch, {
        "search_contacts": search_contacts,
        "list_contacts": lambda a: _env_ok({}),
        "get_contact": get_contact,
    })
    monkeypatch.setattr(contacts_read.llm_router, "generate", lambda req: _LLMResponse("nope"))

    result = contacts_read.generate_contacts_read_response(
        user_id="u1", connection_id="c1",
        message="give me all phone numbers associated with Subhro",
        conversation_id="conv1",
    )
    text = result.get("assistant_response") or ""
    # The wrong number must NOT appear
    assert "+999" not in text
    # The correct one should still appear
    assert "70019" in text
    # Should note partial failure
    assert "Could not retrieve details for" in text
