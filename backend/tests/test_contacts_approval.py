import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest

from app.schemas.action_approval import ApprovalProposalCreate
from app.services.action_approval_service import (
    ActionApprovalService,
    ApprovalConflictError,
    ApprovalExpiredError,
)
from app.services.contacts_action_executor import ContactsActionExecutor


USER_ID = uuid4()
CONNECTION_ID = uuid4()
PROPOSAL_ID = uuid4()
PEOPLE_RESOURCE = "people/c123456789"


class _InMemoryRepo:
    engine = None

    def __init__(self):
        self.row = None

    def create_proposal(self, **kwargs):
        now = datetime.now(timezone.utc)
        operation = kwargs.get("calendar_operation") or {}
        self.row = {
            "proposal_id": kwargs["proposal_id"],
            "message_id": uuid4(),
            "source_message_id": kwargs["source_message_id"],
            "user_id": kwargs["user_id"],
            "conversation_id": kwargs["conversation_id"],
            "action_type": kwargs["action_type"],
            "parameters": kwargs["parameters"],
            "connection_id": kwargs["connection_id"],
            "account_email": kwargs["account_email"],
            "created_at": now,
            "expires_at": now + timedelta(minutes=15),
            "status": "pending",
            "approved_at": None,
            "execution_started_at": None,
            "completed_at": None,
            "payload_fingerprint": operation.get("payload_fingerprint"),
            "operation_id": (
                UUID(str(operation["operation_id"]))
                if operation.get("operation_id")
                else None
            ),
            "integrity_schema_version": operation.get(
                "integrity_schema_version"
            ),
        }
        return dict(self.row)

    def get_proposal(self, user_id, proposal_id):
        if self.row is None:
            return None
        if self.row["proposal_id"] != proposal_id:
            return None
        if self.row["user_id"] != user_id:
            return None
        return self.row

    def transition(
        self,
        *,
        user_id,
        proposal_id,
        expected_status,
        new_status,
        expiry_mode="any",
        timestamp_field=None,
        result=None,
    ):
        if self.row is None:
            return None
        if self.row["proposal_id"] != proposal_id:
            return None
        if self.row["user_id"] != user_id:
            return None
        now = datetime.now(timezone.utc)
        expired = self.row["expires_at"] <= now
        if expiry_mode == "valid" and expired:
            return None
        if expiry_mode == "expired" and not expired:
            return None
        if self.row["status"] != expected_status:
            return None
        self.row["status"] = new_status
        if timestamp_field:
            self.row[timestamp_field] = now
        if result is not None:
            self.row["result"] = result
        return self.row

    def recover_stale_gmail_send(self, **_kwargs):
        return None


def _service(monkeypatch):
    monkeypatch.setattr(
        "app.services.contacts_action_executor.settings.CONTACTS_CREATE_ENABLED",
        True,
    )
    monkeypatch.setattr(
        "app.services.contacts_action_executor.settings.CONTACTS_UPDATE_ENABLED",
        True,
    )
    monkeypatch.setattr(
        "app.services.contacts_action_executor.settings.CONTACTS_DELETE_ENABLED",
        True,
    )
    repo = _InMemoryRepo()
    service = ActionApprovalService(db=None, repository=repo)
    service.connection_repository = SimpleNamespace(
        get_active_google_connection=Mock(
            return_value=SimpleNamespace(email="owner@example.com")
        ),
    )
    service.credential_repository = SimpleNamespace(
        get_by_connection=Mock(
            return_value=SimpleNamespace(
                scopes=["https://www.googleapis.com/auth/contacts"]
            )
        ),
    )
    return service, repo


def _payload(action_type, parameters):
    return ApprovalProposalCreate.model_validate({
        "conversation_id": uuid4(),
        "source_message_id": uuid4(),
        "connection_id": CONNECTION_ID,
        "action_type": action_type,
        "parameters": parameters,
    })


def _approved(service, action_type, parameters):
    payload = _payload(action_type, parameters)
    service.create_proposal(
        user_id=USER_ID,
        payload=payload,
        proposal_id=PROPOSAL_ID,
    )
    service.approve(user_id=USER_ID, proposal_id=PROPOSAL_ID)


def test_contacts_create_approve_execute_uses_mocked_api(monkeypatch):
    service, repo = _service(monkeypatch)
    _approved(
        service,
        "contacts.create_contact",
        {"display_name": "Ada", "email": "ada@example.com"},
    )

    contacts_service = SimpleNamespace(
        create_contact=AsyncMock(return_value={"resourceName": PEOPLE_RESOURCE}),
    )
    executor = ContactsActionExecutor(
        db=None,
        approval_service=service,
        contacts_service=contacts_service,
    )
    result = asyncio.run(executor.execute(user_id=USER_ID, proposal_id=PROPOSAL_ID))
    assert result.status == "completed"
    contacts_service.create_contact.assert_awaited_once()
    kwargs = contacts_service.create_contact.await_args.kwargs
    assert kwargs["user_id"] == USER_ID
    assert kwargs["connection_id"] == CONNECTION_ID
    assert kwargs["contact"]["emailAddresses"] == [{"value": "ada@example.com"}]
    assert kwargs["contact"]["names"][0]["givenName"] == "Ada"
    assert kwargs["contact"]["names"][0]["displayName"] == "Ada"


def test_contacts_disabled_flag_prevents_execution(monkeypatch):
    service, repo = _service(monkeypatch)
    monkeypatch.setattr(
        "app.services.contacts_action_executor.settings.CONTACTS_CREATE_ENABLED",
        False,
    )
    _approved(
        service,
        "contacts.create_contact",
        {"display_name": "Ada", "email": "ada@example.com"},
    )

    contacts_service = SimpleNamespace(create_contact=AsyncMock())
    executor = ContactsActionExecutor(
        db=None,
        approval_service=service,
        contacts_service=contacts_service,
    )
    with pytest.raises(PermissionError):
        asyncio.run(executor.execute(user_id=USER_ID, proposal_id=PROPOSAL_ID))
    contacts_service.create_contact.assert_not_awaited()


def test_contacts_missing_fingerprint_rejected(monkeypatch):
    service, repo = _service(monkeypatch)
    _approved(
        service,
        "contacts.create_contact",
        {"display_name": "Ada", "email": "ada@example.com"},
    )
    repo.row["payload_fingerprint"] = None

    contacts_service = SimpleNamespace(create_contact=AsyncMock())
    executor = ContactsActionExecutor(
        db=None,
        approval_service=service,
        contacts_service=contacts_service,
    )
    with pytest.raises(ValueError, match="integrity verification failed"):
        asyncio.run(executor.execute(user_id=USER_ID, proposal_id=PROPOSAL_ID))
    contacts_service.create_contact.assert_not_awaited()


def test_contacts_tampered_fingerprint_rejected(monkeypatch):
    service, repo = _service(monkeypatch)
    _approved(
        service,
        "contacts.create_contact",
        {"display_name": "Ada", "email": "ada@example.com"},
    )
    repo.row["payload_fingerprint"] = "0" * 64

    contacts_service = SimpleNamespace(create_contact=AsyncMock())
    executor = ContactsActionExecutor(
        db=None,
        approval_service=service,
        contacts_service=contacts_service,
    )
    with pytest.raises(ValueError, match="integrity verification failed"):
        asyncio.run(executor.execute(user_id=USER_ID, proposal_id=PROPOSAL_ID))
    contacts_service.create_contact.assert_not_awaited()


def test_contacts_tampered_payload_rejected(monkeypatch):
    service, repo = _service(monkeypatch)
    _approved(
        service,
        "contacts.create_contact",
        {"display_name": "Ada", "email": "ada@example.com"},
    )
    repo.row["parameters"]["display_name"] = "Tampered"

    contacts_service = SimpleNamespace(create_contact=AsyncMock())
    executor = ContactsActionExecutor(
        db=None,
        approval_service=service,
        contacts_service=contacts_service,
    )
    with pytest.raises(ValueError, match="integrity verification failed"):
        asyncio.run(executor.execute(user_id=USER_ID, proposal_id=PROPOSAL_ID))
    contacts_service.create_contact.assert_not_awaited()


def test_contacts_wrong_user_rejected(monkeypatch):
    service, repo = _service(monkeypatch)
    _approved(
        service,
        "contacts.create_contact",
        {"display_name": "Ada", "email": "ada@example.com"},
    )

    contacts_service = SimpleNamespace(create_contact=AsyncMock())
    executor = ContactsActionExecutor(
        db=None,
        approval_service=service,
        contacts_service=contacts_service,
    )
    with pytest.raises(LookupError):
        asyncio.run(
            executor.execute(user_id=uuid4(), proposal_id=PROPOSAL_ID)
        )
    contacts_service.create_contact.assert_not_awaited()


def test_contacts_expired_approval_rejected(monkeypatch):
    service, repo = _service(monkeypatch)
    payload = _payload(
        "contacts.create_contact",
        {"display_name": "Ada", "email": "ada@example.com"},
    )
    service.create_proposal(
        user_id=USER_ID,
        payload=payload,
        proposal_id=PROPOSAL_ID,
    )
    repo.row["expires_at"] = datetime.now(timezone.utc) - timedelta(minutes=1)

    with pytest.raises(ApprovalExpiredError):
        service.approve(user_id=USER_ID, proposal_id=PROPOSAL_ID)

    contacts_service = SimpleNamespace(create_contact=AsyncMock())
    executor = ContactsActionExecutor(
        db=None,
        approval_service=service,
        contacts_service=contacts_service,
    )
    # Hardened behavior: terminal/non-executable proposals are returned without side effects.
    result = asyncio.run(executor.execute(user_id=USER_ID, proposal_id=PROPOSAL_ID))
    assert result.status in {"expired", "failed", "rejected", "execution_uncertain"}
    contacts_service.create_contact.assert_not_awaited()


def test_contacts_duplicate_execution_returns_existing_status(monkeypatch):
    service, repo = _service(monkeypatch)
    _approved(
        service,
        "contacts.create_contact",
        {"display_name": "Ada", "email": "ada@example.com"},
    )

    contacts_service = SimpleNamespace(
        create_contact=AsyncMock(return_value={"resourceName": PEOPLE_RESOURCE}),
    )
    executor = ContactsActionExecutor(
        db=None,
        approval_service=service,
        contacts_service=contacts_service,
    )
    first = asyncio.run(executor.execute(user_id=USER_ID, proposal_id=PROPOSAL_ID))
    assert first.status == "completed"
    second = asyncio.run(executor.execute(user_id=USER_ID, proposal_id=PROPOSAL_ID))
    assert second.status == "completed"
    assert contacts_service.create_contact.await_count == 1


def test_contacts_disabled_create_flag_blocks(monkeypatch):
    service, repo = _service(monkeypatch)
    monkeypatch.setattr(
        "app.services.contacts_action_executor.settings.CONTACTS_CREATE_ENABLED",
        False,
    )
    _approved(
        service,
        "contacts.create_contact",
        {"display_name": "Ada", "email": "ada@example.com"},
    )

    contacts_service = SimpleNamespace(create_contact=AsyncMock())
    executor = ContactsActionExecutor(
        db=None,
        approval_service=service,
        contacts_service=contacts_service,
    )
    with pytest.raises(PermissionError):
        asyncio.run(executor.execute(user_id=USER_ID, proposal_id=PROPOSAL_ID))
    contacts_service.create_contact.assert_not_awaited()


def test_contacts_disabled_update_flag_blocks(monkeypatch):
    service, repo = _service(monkeypatch)
    monkeypatch.setattr(
        "app.services.contacts_action_executor.settings.CONTACTS_UPDATE_ENABLED",
        False,
    )
    _approved(
        service,
        "contacts.update_contact",
        {"contact_id": PEOPLE_RESOURCE, "display_name": "Ada2", "etag": "e1"},
    )

    contacts_service = SimpleNamespace(
        get_contact=AsyncMock(),
        update_contact=AsyncMock(),
    )
    executor = ContactsActionExecutor(
        db=None,
        approval_service=service,
        contacts_service=contacts_service,
    )
    with pytest.raises(PermissionError):
        asyncio.run(executor.execute(user_id=USER_ID, proposal_id=PROPOSAL_ID))
    contacts_service.update_contact.assert_not_awaited()


def test_contacts_disabled_delete_flag_blocks(monkeypatch):
    service, repo = _service(monkeypatch)
    monkeypatch.setattr(
        "app.services.contacts_action_executor.settings.CONTACTS_DELETE_ENABLED",
        False,
    )
    _approved(
        service,
        "contacts.delete_contact",
        {"contact_id": PEOPLE_RESOURCE},
    )

    contacts_service = SimpleNamespace(
        get_contact=AsyncMock(),
        delete_contact=AsyncMock(),
    )
    executor = ContactsActionExecutor(
        db=None,
        approval_service=service,
        contacts_service=contacts_service,
    )
    with pytest.raises(PermissionError):
        asyncio.run(executor.execute(user_id=USER_ID, proposal_id=PROPOSAL_ID))
    contacts_service.delete_contact.assert_not_awaited()


def test_contacts_update_execute_uses_mocked_api(monkeypatch):
    service, repo = _service(monkeypatch)
    _approved(
        service,
        "contacts.update_contact",
        {"contact_id": PEOPLE_RESOURCE, "display_name": "Ada2", "etag": "etag-1"},
    )

    contacts_service = SimpleNamespace(
        get_contact=AsyncMock(return_value={
            "resource_name": PEOPLE_RESOURCE,
            "etag": "etag-1",
            "raw": {"resourceName": PEOPLE_RESOURCE, "etag": "etag-1"},
        }),
        update_contact=AsyncMock(return_value={"resourceName": PEOPLE_RESOURCE}),
    )
    executor = ContactsActionExecutor(
        db=None,
        approval_service=service,
        contacts_service=contacts_service,
    )
    result = asyncio.run(executor.execute(user_id=USER_ID, proposal_id=PROPOSAL_ID))
    assert result.status == "completed"
    contacts_service.update_contact.assert_awaited_once()
    kwargs = contacts_service.update_contact.await_args.kwargs
    assert kwargs["user_id"] == USER_ID
    assert kwargs["connection_id"] == CONNECTION_ID
    assert kwargs["resource_name"] == PEOPLE_RESOURCE
    assert kwargs["update_person_fields"] == "names"
    assert kwargs["etag"] == "etag-1"
    assert kwargs["contact"]["names"][0]["displayName"] == "Ada2"


def test_contacts_delete_execute_uses_mocked_api(monkeypatch):
    service, repo = _service(monkeypatch)
    _approved(
        service,
        "contacts.delete_contact",
        {"contact_id": PEOPLE_RESOURCE},
    )

    contacts_service = SimpleNamespace(
        get_contact=AsyncMock(return_value={
            "resource_name": PEOPLE_RESOURCE,
            "etag": "etag-1",
            "raw": {"resourceName": PEOPLE_RESOURCE},
        }),
        delete_contact=AsyncMock(return_value={}),
    )
    executor = ContactsActionExecutor(
        db=None,
        approval_service=service,
        contacts_service=contacts_service,
    )
    result = asyncio.run(executor.execute(user_id=USER_ID, proposal_id=PROPOSAL_ID))
    assert result.status == "completed"
    contacts_service.get_contact.assert_awaited_once()
    contacts_service.delete_contact.assert_awaited_once_with(
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        resource_name=PEOPLE_RESOURCE,
    )
