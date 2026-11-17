import asyncio
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.api.v1.endpoints import contacts
from app.db.session import get_db
from app.main import app
from app.schemas.action_approval import ActionApprovalProposal
from app.services.contacts_action_executor import ContactsActionExecutor
from app.services.contacts_action_identity import build_contacts_operation_identity


USER_ID = UUID("00000000-0000-0000-0000-000000000111")
CONNECTION_ID = UUID("00000000-0000-0000-0000-000000000222")
PEOPLE = "people/c123456789"
ETAG = "%EgUBAi43PRoEAQIFByIMd2FyZQ=="


def _proposal(**overrides):
    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    base = dict(
        proposal_id=uuid4(),
        message_id=uuid4(),
        source_message_id=uuid4(),
        user_id=USER_ID,
        conversation_id=uuid4(),
        connection_id=CONNECTION_ID,
        action_type="contacts.update_contact",
        parameters={"contact_id": PEOPLE, "etag": ETAG},
        created_at=now,
        expires_at=now,
        status="pending",
        approved_at=None,
        execution_started_at=None,
        completed_at=None,
        result=None,
        execution_available=True,
        integrity_schema_version=2,
        payload_fingerprint="f" * 64,
    )
    base.update(overrides)
    return ActionApprovalProposal(**base)


def _contacts_access(*, write_connection_id=CONNECTION_ID):
    return {
        "selection_required": False,
        "default_connection_invalid": False,
        "google_connection_available": True,
        "read_connection_id": str(write_connection_id),
        "write_connection_id": str(write_connection_id),
        "read_scope_missing": False,
        "write_scope_missing": False,
        "selected_connection_email": "owner@example.com",
        "candidate_emails": ["owner@example.com"],
        "capabilities": {},
    }


@pytest.fixture
def api_client(monkeypatch):
    app.dependency_overrides[deps.get_current_user] = lambda: {
        "id": str(USER_ID),
        "user_metadata": {"default_contacts_connection_id": str(CONNECTION_ID)},
    }
    app.dependency_overrides[get_db] = lambda: object()
    monkeypatch.setattr(contacts.settings, "CONTACTS_UPDATE_ENABLED", True)
    monkeypatch.setattr(
        contacts.UserConnectionService,
        "get_user_contacts_access",
        lambda self, **kwargs: _contacts_access(),
    )
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _post_update(api_client, monkeypatch, current_raw, params_changes, expected_status=201):
    proposal = _proposal()
    captured = {}

    async def fake_get_contact(self, *, user_id, connection_id, resource_name, person_fields=None):
        return {
            "resource_name": PEOPLE,
            "etag": ETAG,
            "raw": {"resourceName": PEOPLE, "etag": ETAG, **current_raw},
        }

    class ApprovalServiceStub:
        def __init__(self, _db):
            pass

        def create_standalone_contacts_proposal(self, **kwargs):
            captured.update(kwargs)
            # build proposal with captured params so integrity is consistent if needed elsewhere
            p = _proposal(parameters=kwargs["parameters"])
            return p

    monkeypatch.setattr(contacts.ContactsService, "get_contact", fake_get_contact)
    monkeypatch.setattr(contacts, "ActionApprovalService", ApprovalServiceStub)

    payload = {"action_type": "contacts.update_contact", "parameters": {"contact_id": PEOPLE, "etag": ETAG, **params_changes}}
    res = api_client.post("/api/v1/contacts/proposals", json=payload)
    assert res.status_code == expected_status, res.text
    return res, captured


def test_update_only_job_title_preserves_other_fields_and_masks_organizations(api_client, monkeypatch):
    current = {
        "names": [{"displayName": "Rahul", "givenName": "Rahul", "familyName": "K"}],
        "emailAddresses": [{"value": "rahul@example.com"}, {"value": "work@example.com"}],
        "phoneNumbers": [{"value": "111", "metadata": {"primary": True}}],
        "organizations": [{"name": "Acme", "title": "Engineer"}],
    }
    _, captured = _post_update(api_client, monkeypatch, current, {"job_title": "Senior Engineer"})
    plan = captured["parameters"]["_contacts_update_plan"]
    assert plan["update_person_fields"] == "organizations"
    assert plan["contact_patch"] == {"organizations": [{"name": "Acme", "title": "Senior Engineer"}]}
    assert "emailAddresses" not in plan["contact_patch"]
    assert "names" not in plan["contact_patch"]


def test_update_only_organization_name_preserves_job_title(api_client, monkeypatch):
    current = {"organizations": [{"name": "Acme", "title": "Engineer"}]}
    _, captured = _post_update(api_client, monkeypatch, current, {"organization": "Beta"})
    plan = captured["parameters"]["_contacts_update_plan"]
    assert plan["update_person_fields"] == "organizations"
    assert plan["contact_patch"]["organizations"] == [{"name": "Beta", "title": "Engineer"}]


def test_update_one_phone_preserves_unrelated_phone_numbers_when_unambiguous(api_client, monkeypatch):
    current = {
        "phoneNumbers": [
            {"value": "9876543210", "type": "mobile", "metadata": {"primary": True}},
            {"value": "9123456780", "type": "work"},
        ],
        "emailAddresses": [{"value": "a@example.com"}, {"value": "b@example.com"}],
    }
    _, captured = _post_update(api_client, monkeypatch, current, {"phone": "9000000000"})
    plan = captured["parameters"]["_contacts_update_plan"]
    assert plan["update_person_fields"] == "phoneNumbers"
    assert plan["contact_patch"]["phoneNumbers"] == [
        {"value": "9000000000", "type": "mobile", "metadata": {"primary": True}},
        {"value": "9123456780", "type": "work"},
    ]
    assert "emailAddresses" not in plan["contact_patch"]


def test_ambiguous_multi_value_phone_requires_clarification(api_client, monkeypatch):
    current = {
        "phoneNumbers": [
            {"value": "9876543210", "type": "mobile"},
            {"value": "9123456780", "type": "work"},
        ]
    }
    res, _ = _post_update(api_client, monkeypatch, current, {"phone": "9000000000"}, expected_status=422)
    assert "multiple" in res.text.lower() or "clarif" in res.text.lower()


def test_update_given_name_preserves_family_name(api_client, monkeypatch):
    current = {"names": [{"givenName": "Ada", "familyName": "Lovelace", "displayName": "Ada Lovelace"}]}
    _, captured = _post_update(api_client, monkeypatch, current, {"given_name": "Augusta"})
    plan = captured["parameters"]["_contacts_update_plan"]
    assert plan["update_person_fields"] == "names"
    assert plan["contact_patch"]["names"][0]["givenName"] == "Augusta"
    assert plan["contact_patch"]["names"][0]["familyName"] == "Lovelace"


def test_update_family_name_preserves_given_name(api_client, monkeypatch):
    current = {"names": [{"givenName": "Ada", "familyName": "Lovelace", "displayName": "Ada Lovelace"}]}
    _, captured = _post_update(api_client, monkeypatch, current, {"family_name": "Byron"})
    plan = captured["parameters"]["_contacts_update_plan"]
    assert plan["contact_patch"]["names"][0]["givenName"] == "Ada"
    assert plan["contact_patch"]["names"][0]["familyName"] == "Byron"


def test_missing_fields_preserve_existing_values(api_client, monkeypatch):
    current = {
        "names": [{"givenName": "Ada", "familyName": "Lovelace"}],
        "emailAddresses": [{"value": "ada@example.com"}],
        "phoneNumbers": [{"value": "123"}],
    }
    _, captured = _post_update(api_client, monkeypatch, current, {"email": "ada2@example.com"})
    plan = captured["parameters"]["_contacts_update_plan"]
    assert plan["update_person_fields"] == "emailAddresses"
    assert "names" not in plan["contact_patch"]
    assert "phoneNumbers" not in plan["contact_patch"]


def test_empty_update_payload_rejected(api_client, monkeypatch):
    current = {"names": [{"givenName": "Ada"}]}
    payload = {"action_type": "contacts.update_contact", "parameters": {"contact_id": PEOPLE, "etag": ETAG}}
    async def fake_get_contact(self, **kwargs):
        return {"resource_name": PEOPLE, "etag": ETAG, "raw": {"resourceName": PEOPLE, "etag": ETAG, **current}}
    monkeypatch.setattr(contacts.ContactsService, "get_contact", fake_get_contact)
    res = api_client.post("/api/v1/contacts/proposals", json=payload)
    assert res.status_code == 422


def test_empty_strings_do_not_silently_clear_fields(api_client, monkeypatch):
    current = {"emailAddresses": [{"value": "ada@example.com"}]}
    payload = {"action_type": "contacts.update_contact", "parameters": {"contact_id": PEOPLE, "etag": ETAG, "email": ""}}
    async def fake_get_contact(self, **kwargs):
        return {"resource_name": PEOPLE, "etag": ETAG, "raw": {"resourceName": PEOPLE, "etag": ETAG, **current}}
    monkeypatch.setattr(contacts.ContactsService, "get_contact", fake_get_contact)
    res = api_client.post("/api/v1/contacts/proposals", json=payload)
    assert res.status_code == 422


def test_explicit_clear_email_works_only_when_requested(api_client, monkeypatch):
    current = {"emailAddresses": [{"value": "ada@example.com"}], "phoneNumbers": [{"value": "123"}]}
    _, captured = _post_update(api_client, monkeypatch, current, {"clear_fields": ["email"]})
    plan = captured["parameters"]["_contacts_update_plan"]
    assert plan["update_person_fields"] == "emailAddresses"
    assert plan["contact_patch"]["emailAddresses"] == []
    assert "phoneNumbers" not in plan["contact_patch"]


def test_invalid_clear_request_rejected(api_client, monkeypatch):
    current = {"names": [{"givenName": "Ada"}]}
    res, _ = _post_update(api_client, monkeypatch, current, {"clear_fields": ["display_name"]}, expected_status=422)
    assert res.status_code == 422


def test_field_mask_contains_only_intended_fields(api_client, monkeypatch):
    current = {"phoneNumbers": [{"value": "1"}], "organizations": [{"name": "Acme", "title": "Eng"}]}
    _, captured = _post_update(api_client, monkeypatch, current, {"phone": "2", "job_title": "Sr Eng"})
    plan = captured["parameters"]["_contacts_update_plan"]
    assert plan["update_person_fields"] == "phoneNumbers,organizations"


def test_executor_uses_normalized_plan_not_client_reconstruction(monkeypatch):
    monkeypatch.setattr(
        "app.services.contacts_action_executor.settings.CONTACTS_UPDATE_ENABLED", True
    )
    svc = SimpleNamespace(get_proposal=None, claim_execution=None, finish_execution=None)
    params = {
        "contact_id": PEOPLE,
        "etag": ETAG,
        # client-like flat field present but plan is authoritative
        "display_name": "SHOULD_NOT_BE_USED",
        "_contacts_update_plan": {
            "schema_version": 1,
            "update_person_fields": "organizations",
            "contact_patch": {"organizations": [{"name": "Acme", "title": "Lead"}]},
        },
    }
    identity = build_contacts_operation_identity(
        proposal_id=uuid4(), user_id=USER_ID, connection_id=CONNECTION_ID,
        action_type="contacts.update_contact", resource_name=PEOPLE, parameters=params,
    )
    proposal = SimpleNamespace(
        proposal_id=identity.proposal_id, user_id=USER_ID, connection_id=CONNECTION_ID,
        action_type="contacts.update_contact", parameters=params, status="approved",
        payload_fingerprint=identity.payload_fingerprint,
    )
    svc.get_proposal = lambda user_id, proposal_id: proposal
    svc.claim_execution = lambda **kwargs: proposal
    finished = {}
    def finish_execution(**kwargs):
        finished.update(kwargs)
        return SimpleNamespace(status="completed")
    svc.finish_execution = finish_execution

    calls = {}
    async def get_contact(**kwargs):
        return {"resource_name": PEOPLE, "etag": ETAG, "raw": {"etag": ETAG}}
    async def update_contact(**kwargs):
        calls.update(kwargs)
        return {"resourceName": PEOPLE}

    contacts_svc = SimpleNamespace(get_contact=get_contact, update_contact=update_contact)
    executor = ContactsActionExecutor(db=None, approval_service=svc, contacts_service=contacts_svc)
    result = asyncio.run(executor.execute(user_id=USER_ID, proposal_id=proposal.proposal_id))
    assert calls["update_person_fields"] == "organizations"
    assert calls["contact"] == {"organizations": [{"name": "Acme", "title": "Lead"}]}
    assert calls["resource_name"] == PEOPLE
    assert calls["etag"] == ETAG
