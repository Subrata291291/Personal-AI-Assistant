"""Phase 10: live-data provenance and no-fabrication regressions.

These tests use clearly-marked mock People API payloads. They never call live
Google APIs and never assert that production data has a particular value.
"""

import json
import logging

from app.workflows import contacts_read


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
    monkeypatch.setattr(
        contacts_read,
        "create_contacts_tools",
        lambda user_id, connection_id: tools,
    )
    monkeypatch.setattr(
        contacts_read,
        "extract_contacts_read_intent_llm",
        lambda message: None,
    )
    return tools


SUSPICIOUS_SNIPPET = {
    "resource_name": "people/mock-paromita-001",
    "display_name": "Paromita Di",
    "primary_phone": "+1 (555) 123-4567",
    "phones": [{"value": "+1 (555) 123-4567", "primary": True}],
    "primary_email": "paromita.di@example.com",
    "emails": [{"value": "paromita.di@example.com", "primary": True}],
    "organization": "XYZ Corp",
    "job_title": "Engineer",
    "addresses": [
        {
            "formatted": "123 Main St, Springfield, IL 62704",
            "primary": True,
        }
    ],
}

AUTHORITATIVE_CLEAN = {
    "resource_name": "people/mock-paromita-001",
    "display_name": "Paromita Di",
    "primary_phone": "+8801700000123",
    "phones": [{"value": "+8801700000123", "primary": True}],
    "primary_email": "paromita.real@trustedco.com",
    "emails": [{"value": "paromita.real@trustedco.com", "primary": True}],
    "organization": "Trusted Org",
    "job_title": "Engineer",
    "addresses": [{"formatted": "Dhaka, Bangladesh", "primary": True}],
}

AUTHORITATIVE_SUSPICIOUS = dict(SUSPICIOUS_SNIPPET)

AUTHORITATIVE_MISSING_FIELDS = {
    "resource_name": "people/mock-paromita-001",
    "display_name": "Paromita Di",
}

AUTHORITATIVE_OTHER_CONTACT = {
    "resource_name": "people/mock-other-999",
    "display_name": "Other Person",
    "primary_phone": "+8801999999999",
    "phones": [{"value": "+8801999999999", "primary": True}],
}


def _assert_suspicious_values_absent(text: str) -> None:
    assert "+1 (555) 123-4567" not in text
    assert "paromita.di@example.com" not in text
    assert "XYZ Corp" not in text
    assert "123 Main St" not in text
    assert "Springfield" not in text


def test_search_snippet_suspicious_details_are_not_rendered_when_authoritative_is_clean(monkeypatch):
    def search_contacts(args):
        return _env_ok({"contacts": [SUSPICIOUS_SNIPPET]})

    def get_contact(args):
        assert args["resource_name"] == SUSPICIOUS_SNIPPET["resource_name"]
        return _env_ok(AUTHORITATIVE_CLEAN)

    _mock_tools(monkeypatch, {
        "search_contacts": search_contacts,
        "list_contacts": lambda _a: _env_ok({}),
        "get_contact": get_contact,
    })

    result = contacts_read.generate_contacts_read_response(
        user_id="user-1",
        connection_id="conn-1",
        message="Show details for Paromita Di",
    )

    response = result["assistant_response"]
    assert AUTHORITATIVE_CLEAN["primary_phone"] in response
    assert AUTHORITATIVE_CLEAN["primary_email"] in response
    assert AUTHORITATIVE_CLEAN["organization"] in response
    assert AUTHORITATIVE_CLEAN["addresses"][0]["formatted"] in response
    _assert_suspicious_values_absent(response)

    provenance = result.get("contacts_provenance") or {}
    assert provenance.get("verified_source") is True
    assert provenance.get("outcome_status") == "single_match"
    assert provenance.get("selected_contact_resource_ref") == contacts_read._safe_ref(
        SUSPICIOUS_SNIPPET["resource_name"]
    )


def test_authoritative_suspicious_patterns_log_only_safe_categories(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="app.workflows.contacts_read")

    def search_contacts(args):
        return _env_ok({"contacts": [AUTHORITATIVE_SUSPICIOUS]})

    def get_contact(args):
        return _env_ok(AUTHORITATIVE_SUSPICIOUS)

    _mock_tools(monkeypatch, {
        "search_contacts": search_contacts,
        "list_contacts": lambda _a: _env_ok({}),
        "get_contact": get_contact,
    })

    result = contacts_read.generate_contacts_read_response(
        user_id="user-1",
        connection_id="conn-1",
        message="Show details for Paromita Di",
    )

    # The authoritative payload is rendered, but the warning log must not
    # contain any raw phone/email/address/organization value.
    assert "+1 (555) 123-4567" in result["assistant_response"]
    log_text = caplog.text
    assert "contacts_suspicious_data_detected" in log_text
    assert "field=phone" in log_text
    assert "pattern=555_test_number" in log_text
    assert "field=email" in log_text
    assert "pattern=example_or_test_domain" in log_text
    assert "field=organization" in log_text
    assert "pattern=test_org" in log_text
    assert "field=address" in log_text
    assert "pattern=test_address" in log_text

    assert "+1 (555) 123-4567" not in log_text
    assert "paromita.di@example.com" not in log_text
    assert "XYZ Corp" not in log_text
    assert "123 Main St" not in log_text
    assert "Springfield" not in log_text


def test_suspicious_pattern_helper_returns_only_field_and_category():
    findings = contacts_read._suspicious_contact_patterns(SUSPICIOUS_SNIPPET)

    assert ("phone", "555_test_number") in findings
    assert ("email", "example_or_test_domain") in findings
    assert ("organization", "test_org") in findings
    assert ("address", "test_address") in findings
    for field, pattern in findings:
        assert field in {"phone", "email", "organization", "address"}
        assert isinstance(pattern, str)
        assert "@" not in pattern
        assert "+1" not in pattern
        assert "XYZ" not in pattern
        assert "Main St" not in pattern


def test_authoritative_missing_fields_render_as_not_available(monkeypatch):
    def search_contacts(args):
        return _env_ok({"contacts": [AUTHORITATIVE_MISSING_FIELDS]})

    def get_contact(args):
        return _env_ok(AUTHORITATIVE_MISSING_FIELDS)

    _mock_tools(monkeypatch, {
        "search_contacts": search_contacts,
        "list_contacts": lambda _a: _env_ok({}),
        "get_contact": get_contact,
    })

    result = contacts_read.generate_contacts_read_response(
        user_id="user-1",
        connection_id="conn-1",
        message="Show details for Paromita Di",
    )

    response = result["assistant_response"]
    assert "Phone: Not available" in response
    assert "Email: Not available" in response
    assert "Address: Not available" in response
    assert "@" not in response
    _assert_suspicious_values_absent(response)


def test_authoritative_resource_name_mismatch_fails_closed(monkeypatch):
    def search_contacts(args):
        return _env_ok({"contacts": [SUSPICIOUS_SNIPPET]})

    def get_contact(args):
        assert args["resource_name"] == SUSPICIOUS_SNIPPET["resource_name"]
        return _env_ok(AUTHORITATIVE_OTHER_CONTACT)

    _mock_tools(monkeypatch, {
        "search_contacts": search_contacts,
        "list_contacts": lambda _a: _env_ok({}),
        "get_contact": get_contact,
    })

    result = contacts_read.generate_contacts_read_response(
        user_id="user-1",
        connection_id="conn-1",
        message="Show details for Paromita Di",
    )

    response = result["assistant_response"]
    assert "couldn" in response.casefold() or "try again" in response.casefold()
    assert AUTHORITATIVE_OTHER_CONTACT["primary_phone"] not in response
    _assert_suspicious_values_absent(response)

    provenance = result.get("contacts_provenance") or {}
    assert provenance.get("verified_source") is False
    assert provenance.get("outcome_status") == "error"


def test_authoritative_retrieval_failure_fails_closed(monkeypatch):
    def search_contacts(args):
        return _env_ok({"contacts": [SUSPICIOUS_SNIPPET]})

    def get_contact(args):
        return _env_err("People API unavailable")

    _mock_tools(monkeypatch, {
        "search_contacts": search_contacts,
        "list_contacts": lambda _a: _env_ok({}),
        "get_contact": get_contact,
    })

    result = contacts_read.generate_contacts_read_response(
        user_id="user-1",
        connection_id="conn-1",
        message="Show details for Paromita Di",
    )

    response = result["assistant_response"]
    assert "couldn" in response.casefold() or "try again" in response.casefold()
    _assert_suspicious_values_absent(response)

    provenance = result.get("contacts_provenance") or {}
    assert provenance.get("verified_source") is False
    assert provenance.get("outcome_status") == "error"


def test_llm_cannot_introduce_unsupported_contact_facts(monkeypatch):
    calls = {"llm": 0}

    def fabricate(request):
        calls["llm"] += 1
        return _LLMResponse(
            "Paromita Di, +1 (555) 987-6543, fake@example.com, "
            "Fake Corp, 999 Fake Street, Springfield"
        )

    monkeypatch.setattr(contacts_read.llm_router, "generate", fabricate)

    def search_contacts(args):
        return _env_ok({"contacts": [SUSPICIOUS_SNIPPET]})

    def get_contact(args):
        return _env_ok(AUTHORITATIVE_CLEAN)

    _mock_tools(monkeypatch, {
        "search_contacts": search_contacts,
        "list_contacts": lambda _a: _env_ok({}),
        "get_contact": get_contact,
    })

    result = contacts_read.generate_contacts_read_response(
        user_id="user-1",
        connection_id="conn-1",
        message="Show details for Paromita Di",
    )

    response = result["assistant_response"]
    assert calls["llm"] == 0
    assert AUTHORITATIVE_CLEAN["primary_phone"] in response
    assert AUTHORITATIVE_CLEAN["primary_email"] in response
    assert "fake@example.com" not in response
    assert "+1 (555) 987-6543" not in response
    assert "Fake Corp" not in response
    assert "999 Fake Street" not in response


def test_multiple_matches_all_does_not_mix_fields_across_contacts(monkeypatch):
    contact_a = {
        "resource_name": "people/mock-paromita-a",
        "display_name": "Paromita Di",
        "primary_phone": "+8801700000001",
        "phones": [{"value": "+8801700000001", "primary": True}],
        "primary_email": "a.paromita@trustedco.com",
        "emails": [{"value": "a.paromita@trustedco.com", "primary": True}],
    }
    contact_b = {
        "resource_name": "people/mock-paromita-b",
        "display_name": "Paromita Di",
        "primary_email": "b.paromita@trustedco.com",
        "emails": [{"value": "b.paromita@trustedco.com", "primary": True}],
    }

    def search_contacts(args):
        return _env_ok({"contacts": [contact_a, contact_b]})

    def get_contact(args):
        if args["resource_name"] == contact_a["resource_name"]:
            return _env_ok(contact_a)
        if args["resource_name"] == contact_b["resource_name"]:
            return _env_ok(contact_b)
        return _env_err("unexpected resource")

    _mock_tools(monkeypatch, {
        "search_contacts": search_contacts,
        "list_contacts": lambda _a: _env_ok({}),
        "get_contact": get_contact,
    })

    result = contacts_read.generate_contacts_read_response(
        user_id="user-1",
        connection_id="conn-1",
        message="Show all phone numbers for Paromita Di",
    )

    response = result["assistant_response"]
    assert "+8801700000001" in response
    assert response.count("+8801700000001") == 1
    # Contact B has no phone and must not inherit contact A's phone or leak
    # its own email into a phone-only response.
    assert "b.paromita@trustedco.com" not in response
    assert "No phone number available" in response
    _assert_suspicious_values_absent(response)

    provenance = result.get("contacts_provenance") or {}
    assert provenance.get("outcome_status") == "multiple_matches_all"
    assert provenance.get("verified_source") is True
