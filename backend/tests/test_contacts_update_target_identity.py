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
from app.integrations.google.exceptions import GoogleAPIError
from app.services.contacts_action_identity import (
    build_contacts_operation_identity,
    verify_contacts_proposal_integrity,
)


USER_ID = UUID("00000000-0000-0000-0000-000000000111")
CONNECTION_ID = UUID("00000000-0000-0000-0000-000000000222")
PEOPLE = "people/c123456789"
SERVER_ETAG = "%EgUBAi43PRoEAQIFByIMd2FyZQ=="
CLIENT_OLD_ETAG = "%EgUBAi43PRoEAQIFByIMb2xk"


def _proposal(**overrides):
    now = datetime.now(timezone.utc)
    base = dict(
        proposal_id=uuid4(),
        message_id=uuid4(),
        source_message_id=uuid4(),
        user_id=USER_ID,
        conversation_id=uuid4(),
        connection_id=CONNECTION_ID,
        action_type="contacts.update_contact",
        parameters={"contact_id": PEOPLE, "etag": SERVER_ETAG, "display_name": "Ada 2"},
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


def _update_payload(contact_id=PEOPLE, etag=CLIENT_OLD_ETAG, **changes):
    params = {"contact_id": contact_id, "etag": etag}
    params.update(changes)
    return {"action_type": "contacts.update_contact", "parameters": params}


def test_update_proposal_fetches_target_and_binds_server_resource_and_etag(api_client, monkeypatch):
    proposal = _proposal()
    lookups = []

    async def fake_get_contact(self, *, user_id, connection_id, resource_name, person_fields=None):
        lookups.append((user_id, connection_id, resource_name, person_fields))
        return {"resource_name": PEOPLE, "etag": SERVER_ETAG, "raw": {"resourceName": PEOPLE, "etag": SERVER_ETAG}}

    captured = {}

    class ApprovalServiceStub:
        def __init__(self, _db):
            pass

        def create_standalone_contacts_proposal(self, **kwargs):
            captured.update(kwargs)
            return proposal

    monkeypatch.setattr(contacts.ContactsService, "get_contact", fake_get_contact)
    monkeypatch.setattr(contacts, "ActionApprovalService", ApprovalServiceStub)

    res = api_client.post(
        "/api/v1/contacts/proposals",
        json=_update_payload(etag=SERVER_ETAG, display_name="Ada 2"),
    )

    assert res.status_code == 201
    assert lookups == [(USER_ID, CONNECTION_ID, PEOPLE, "names,emailAddresses,phoneNumbers,organizations,addresses,metadata")]
    assert captured["user_id"] == USER_ID
    assert captured["connection_id"] == CONNECTION_ID
    assert captured["action_type"] == "contacts.update_contact"
    assert captured["parameters"]["contact_id"] == PEOPLE
    assert captured["parameters"]["etag"] == SERVER_ETAG  # server authoritative, not trusted from client beyond match
    assert captured["parameters"]["display_name"] == "Ada 2"


def test_stale_client_etag_returns_409_and_creates_no_proposal(api_client, monkeypatch):
    async def fake_get_contact(self, **kwargs):
        return {"resource_name": PEOPLE, "etag": SERVER_ETAG, "raw": {"resourceName": PEOPLE, "etag": SERVER_ETAG}}

    called = {"create": 0}

    class ApprovalServiceStub:
        def __init__(self, _db):
            pass

        def create_standalone_contacts_proposal(self, **kwargs):
            called["create"] += 1
            return _proposal()

    monkeypatch.setattr(contacts.ContactsService, "get_contact", fake_get_contact)
    monkeypatch.setattr(contacts, "ActionApprovalService", ApprovalServiceStub)

    res = api_client.post(
        "/api/v1/contacts/proposals",
        json=_update_payload(etag=CLIENT_OLD_ETAG, display_name="Ada 2"),
    )
    assert res.status_code == 409
    detail = res.json().get("detail", {})
    message = detail.get("message") if isinstance(detail, dict) else str(detail)
    assert "refresh" in message.lower() or "changed since" in message.lower()
    assert called["create"] == 0


@pytest.mark.parametrize("contact_result", [
    {"resource_name": "people/different", "etag": SERVER_ETAG},
    {"resource_name": None, "etag": SERVER_ETAG},
    {"resource_name": PEOPLE, "etag": None},
    {},
])
def test_mismatched_or_invalid_verified_target_rejected_no_proposal(api_client, monkeypatch, contact_result):
    async def fake_get_contact(self, **kwargs):
        return contact_result

    called = {"create": 0}

    class ApprovalServiceStub:
        def __init__(self, _db):
            pass

        def create_standalone_contacts_proposal(self, **kwargs):
            called["create"] += 1
            return _proposal()

    monkeypatch.setattr(contacts.ContactsService, "get_contact", fake_get_contact)
    monkeypatch.setattr(contacts, "ActionApprovalService", ApprovalServiceStub)

    res = api_client.post(
        "/api/v1/contacts/proposals",
        json=_update_payload(etag=SERVER_ETAG, display_name="Ada 2"),
    )
    assert res.status_code == 404
    assert called["create"] == 0


def test_nonexistent_contact_rejected_no_proposal(api_client, monkeypatch):
    async def fake_get_contact(self, **kwargs):
        raise GoogleAPIError(status_code=404, message="not found")

    called = {"create": 0}

    class ApprovalServiceStub:
        def __init__(self, _db):
            pass

        def create_standalone_contacts_proposal(self, **kwargs):
            called["create"] += 1
            return _proposal()

    monkeypatch.setattr(contacts.ContactsService, "get_contact", fake_get_contact)
    monkeypatch.setattr(contacts, "ActionApprovalService", ApprovalServiceStub)

    res = api_client.post(
        "/api/v1/contacts/proposals",
        json=_update_payload(etag=SERVER_ETAG, display_name="Ada 2"),
    )
    assert res.status_code == 404
    assert called["create"] == 0


@pytest.mark.parametrize("status_code,expected", [
    (403, 403),
    (429, 503),
    (500, 503),
])
def test_google_lookup_errors_handled_safely_no_proposal(api_client, monkeypatch, status_code, expected):
    async def fake_get_contact(self, **kwargs):
        raise GoogleAPIError(status_code=status_code, message="google error", retryable=(status_code >= 500))

    called = {"create": 0}

    class ApprovalServiceStub:
        def __init__(self, _db):
            pass

        def create_standalone_contacts_proposal(self, **kwargs):
            called["create"] += 1
            return _proposal()

    monkeypatch.setattr(contacts.ContactsService, "get_contact", fake_get_contact)
    monkeypatch.setattr(contacts, "ActionApprovalService", ApprovalServiceStub)

    res = api_client.post(
        "/api/v1/contacts/proposals",
        json=_update_payload(etag=SERVER_ETAG, display_name="Ada 2"),
    )
    assert res.status_code == expected
    assert called["create"] == 0


def test_lookup_timeout_creates_no_proposal(api_client, monkeypatch):
    async def fake_get_contact(self, **kwargs):
        raise TimeoutError("timed out")

    called = {"create": 0}

    class ApprovalServiceStub:
        def __init__(self, _db):
            pass

        def create_standalone_contacts_proposal(self, **kwargs):
            called["create"] += 1
            return _proposal()

    monkeypatch.setattr(contacts.ContactsService, "get_contact", fake_get_contact)
    monkeypatch.setattr(contacts, "ActionApprovalService", ApprovalServiceStub)

    res = api_client.post(
        "/api/v1/contacts/proposals",
        json=_update_payload(etag=SERVER_ETAG, display_name="Ada 2"),
    )
    assert res.status_code == 503
    assert called["create"] == 0


def test_contacts_update_disabled_flag_still_enforced(api_client, monkeypatch):
    monkeypatch.setattr(contacts.settings, "CONTACTS_UPDATE_ENABLED", False)

    async def fake_get_contact(self, **kwargs):
        raise AssertionError("must not verify when flag disabled")

    monkeypatch.setattr(contacts.ContactsService, "get_contact", fake_get_contact)
    res = api_client.post(
        "/api/v1/contacts/proposals",
        json=_update_payload(etag=SERVER_ETAG, display_name="Ada 2"),
    )
    assert res.status_code == 403


def test_existing_create_and_delete_proposal_paths_unchanged(api_client, monkeypatch):
    # Create path: idempotent flow should be used when enabled.
    monkeypatch.setattr(contacts.settings, "CONTACTS_CREATE_ENABLED", True)
    create_proposal = _proposal(action_type="contacts.create_contact", parameters={"display_name": "Ada", "email": "ada@example.com"})

    class CreateIdemStub:
        def __init__(self, _db):
            pass

        def create_contacts_create_proposal(self, **kwargs):
            return SimpleNamespace(proposal=create_proposal, replayed=False)

    monkeypatch.setattr(contacts, "ContactsIdempotencyService", CreateIdemStub)
    res_create = api_client.post(
        "/api/v1/contacts/proposals",
        json={"action_type": "contacts.create_contact", "parameters": {"display_name": "Ada", "email": "ada@example.com"}},
        headers={"Idempotency-Key": "intent-1"},
    )
    assert res_create.status_code == 201

    # Delete path: no target verification/fetch added in this phase.
    monkeypatch.setattr(contacts.settings, "CONTACTS_DELETE_ENABLED", True)
    delete_proposal = _proposal(action_type="contacts.delete_contact", parameters={"contact_id": PEOPLE})
    called = {"create": 0, "lookup": 0}

    async def fake_get_contact(self, **kwargs):
        called["lookup"] += 1
        return {"resource_name": PEOPLE, "etag": SERVER_ETAG}

    class ApprovalServiceStub:
        def __init__(self, _db):
            pass

        def create_standalone_contacts_proposal(self, **kwargs):
            called["create"] += 1
            assert kwargs["action_type"] == "contacts.delete_contact"
            return delete_proposal

    monkeypatch.setattr(contacts.ContactsService, "get_contact", fake_get_contact)
    monkeypatch.setattr(contacts, "ActionApprovalService", ApprovalServiceStub)

    res_delete = api_client.post(
        "/api/v1/contacts/proposals",
        json={"action_type": "contacts.delete_contact", "parameters": {"contact_id": PEOPLE}},
    )
    assert res_delete.status_code == 201
    assert called["create"] == 1
    assert called["lookup"] == 0  # no update-target verification lookup


def test_verify_contacts_proposal_integrity_uses_contact_id_for_update():
    params = {"contact_id": PEOPLE, "etag": SERVER_ETAG, "display_name": "Ada 2"}
    identity = build_contacts_operation_identity(
        proposal_id=uuid4(),
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        action_type="contacts.update_contact",
        resource_name=PEOPLE,
        parameters=params,
    )
    proposal = SimpleNamespace(
        proposal_id=identity.proposal_id,
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        action_type="contacts.update_contact",
        parameters=params,
        payload_fingerprint=identity.payload_fingerprint,
    )
    verified = verify_contacts_proposal_integrity(proposal, USER_ID)
    assert verified.resource_name == PEOPLE
