from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.api.v1.endpoints import contacts
from app.db.session import get_db
from app.main import app
from app.schemas.action_approval import ActionApprovalProposal


USER_ID = UUID("00000000-0000-0000-0000-000000000111")
OTHER_USER_ID = UUID("00000000-0000-0000-0000-000000000999")
CONNECTION_ID = UUID("00000000-0000-0000-0000-000000000222")
OTHER_CONNECTION_ID = UUID("00000000-0000-0000-0000-000000000333")


def _proposal(**overrides):
    now = datetime.now(timezone.utc)
    base = dict(
        proposal_id=uuid4(),
        message_id=uuid4(),
        source_message_id=uuid4(),
        user_id=USER_ID,
        conversation_id=uuid4(),
        connection_id=CONNECTION_ID,
        action_type="contacts.create_contact",
        parameters={"display_name": "Ada", "email": "ada@example.com"},
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


def _contacts_access(*, write_connection_id=CONNECTION_ID, read_connection_id=CONNECTION_ID):
    return {
        "selection_required": False,
        "default_connection_invalid": False,
        "google_connection_available": True,
        "read_connection_id": str(read_connection_id),
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
    monkeypatch.setattr(contacts.settings, "CONTACTS_CREATE_ENABLED", True)
    monkeypatch.setattr(contacts.settings, "CONTACTS_UPDATE_ENABLED", True)
    monkeypatch.setattr(contacts.settings, "CONTACTS_DELETE_ENABLED", True)
    monkeypatch.setattr(
        contacts.UserConnectionService,
        "get_user_contacts_access",
        lambda self, **kwargs: _contacts_access(),
    )
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _create_payload(action_type="contacts.create_contact", parameters=None):
    if parameters is None:
        parameters = {"display_name": "Ada", "email": "ada@example.com"}
    return {"action_type": action_type, "parameters": parameters}


def test_missing_idempotency_key_rejected_for_contacts_create(api_client):
    res = api_client.post("/api/v1/contacts/proposals", json=_create_payload())
    assert res.status_code == 422
    assert "Idempotency-Key" in res.json().get("detail", "")


def test_invalid_idempotency_key_rejected_for_contacts_create(api_client):
    res = api_client.post(
        "/api/v1/contacts/proposals",
        json=_create_payload(),
        headers={"Idempotency-Key": "bad key with spaces"},
    )
    assert res.status_code == 422


def test_new_contacts_create_proposal_returns_201(api_client, monkeypatch):
    proposal = _proposal()

    class ServiceStub:
        def __init__(self, _db):
            pass

        def create_contacts_create_proposal(self, **kwargs):
            assert kwargs["user_id"] == USER_ID
            assert kwargs["connection_id"] == CONNECTION_ID
            assert kwargs["context"].idempotency_key == "intent-1"
            return SimpleNamespace(proposal=proposal, replayed=False)

    monkeypatch.setattr(contacts, "ContactsIdempotencyService", ServiceStub)
    res = api_client.post(
        "/api/v1/contacts/proposals",
        json=_create_payload(),
        headers={"Idempotency-Key": "intent-1"},
    )
    assert res.status_code == 201
    assert res.json()["proposal"]["proposal_id"] == str(proposal.proposal_id)


@pytest.mark.parametrize("status_value", [
    "pending", "approved", "executing", "completed", "failed", "rejected", "expired", "execution_uncertain",
])
def test_same_key_same_payload_replay_returns_existing_status(api_client, monkeypatch, status_value):
    proposal = _proposal(status=status_value)

    class ServiceStub:
        def __init__(self, _db):
            pass

        def create_contacts_create_proposal(self, **kwargs):
            return SimpleNamespace(proposal=proposal, replayed=True)

    monkeypatch.setattr(contacts, "ContactsIdempotencyService", ServiceStub)
    res = api_client.post(
        "/api/v1/contacts/proposals",
        json=_create_payload(),
        headers={"Idempotency-Key": "intent-1"},
    )
    assert res.status_code == 200
    body = res.json()["proposal"]
    assert body["proposal_id"] == str(proposal.proposal_id)
    assert body["status"] == status_value


def test_same_key_different_payload_returns_409(api_client, monkeypatch):
    from app.services.contacts_idempotency_service import ContactsIdempotencyConflictError

    class ServiceStub:
        def __init__(self, _db):
            pass

        def create_contacts_create_proposal(self, **kwargs):
            raise ContactsIdempotencyConflictError("This Idempotency-Key was already used with a different Contacts create request.")

    monkeypatch.setattr(contacts, "ContactsIdempotencyService", ServiceStub)
    res = api_client.post(
        "/api/v1/contacts/proposals",
        json=_create_payload(parameters={"display_name": "Ada", "email": "different@example.com"}),
        headers={"Idempotency-Key": "intent-1"},
    )
    assert res.status_code == 409


def test_contacts_create_feature_flag_disabled(api_client, monkeypatch):
    monkeypatch.setattr(contacts.settings, "CONTACTS_CREATE_ENABLED", False)
    res = api_client.post(
        "/api/v1/contacts/proposals",
        json=_create_payload(),
        headers={"Idempotency-Key": "intent-1"},
    )
    assert res.status_code == 403


def test_update_and_delete_proposals_remain_unchanged_without_idempotency_header(api_client, monkeypatch):
    update_proposal = _proposal(action_type="contacts.update_contact", parameters={"contact_id": "people/c1", "etag": "e1", "display_name": "Ada"})
    delete_proposal = _proposal(action_type="contacts.delete_contact", parameters={"contact_id": "people/c1"})
    calls = []

    async def fake_get_contact(self, *, user_id, connection_id, resource_name, person_fields=None):
        return {"resource_name": "people/c1", "etag": "e1", "raw": {"resourceName": "people/c1", "etag": "e1"}}

    class ApprovalServiceStub:
        def __init__(self, _db):
            pass

        def create_standalone_contacts_proposal(self, **kwargs):
            calls.append(kwargs)
            if kwargs["action_type"] == "contacts.update_contact":
                return update_proposal
            return delete_proposal

    monkeypatch.setattr(contacts.settings, "CONTACTS_UPDATE_ENABLED", True)
    monkeypatch.setattr(contacts.settings, "CONTACTS_DELETE_ENABLED", True)
    monkeypatch.setattr(contacts.ContactsService, "get_contact", fake_get_contact)
    monkeypatch.setattr(contacts, "ActionApprovalService", ApprovalServiceStub)

    res_update = api_client.post(
        "/api/v1/contacts/proposals",
        json={"action_type": "contacts.update_contact", "parameters": {"contact_id": "people/c1", "etag": "e1", "display_name": "Ada"}},
    )
    assert res_update.status_code == 201
    assert res_update.json()["proposal"]["action_type"] == "contacts.update_contact"

    res_delete = api_client.post(
        "/api/v1/contacts/proposals",
        json={"action_type": "contacts.delete_contact", "parameters": {"contact_id": "people/c1"}},
    )
    assert res_delete.status_code == 201
    assert res_delete.json()["proposal"]["action_type"] == "contacts.delete_contact"
    assert [c["action_type"] for c in calls] == ["contacts.update_contact", "contacts.delete_contact"]


def test_server_resolved_connection_used_and_calendar_fallback_not_possible(api_client, monkeypatch):
    captured = {}

    class ServiceStub:
        def __init__(self, _db):
            pass

        def create_contacts_create_proposal(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(proposal=_proposal(), replayed=False)

    monkeypatch.setattr(contacts, "ContactsIdempotencyService", ServiceStub)
    res = api_client.post(
        "/api/v1/contacts/proposals",
        json=_create_payload(),
        headers={"Idempotency-Key": "intent-1"},
    )
    assert res.status_code == 201
    assert captured["connection_id"] == CONNECTION_ID
    assert captured["context"].connection_id == CONNECTION_ID
