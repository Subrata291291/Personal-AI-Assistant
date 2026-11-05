from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api import deps
from app.api.v1.endpoints.action_approvals import get_action_approval_service
from app.db.session import get_db
from app.main import app
from app.schemas.action_approval import (
    ActionApprovalProposal,
    ApprovalProposalCreate,
)
from app.services.action_approval_service import ActionApprovalService


USER_ID = uuid4()
VALID_REQUEST = {
    "conversation_id": "485bc3f9-2b55-4022-a5b4-619f2be167c8",
    "source_message_id": "535bc3f9-2b55-4022-a5b4-619f2be167c8",
    "connection_id": "635bc3f9-2b55-4022-a5b4-619f2be167c8",
    "action_type": "gmail.send",
    "parameters": {
        "to": ["recipient@example.com"],
        "subject": "Reviewed subject",
        "body": "Reviewed message",
    },
}


def _proposal(user_id, payload):
    now = datetime.now(timezone.utc)
    return ActionApprovalProposal(
        proposal_id=uuid4(),
        message_id=uuid4(),
        source_message_id=payload.source_message_id,
        user_id=user_id,
        conversation_id=payload.conversation_id,
        action_type=payload.action_type,
        parameters=payload.parameters,
        connection_id=payload.connection_id,
        account_email="sender@example.com",
        created_at=now,
        expires_at=now + timedelta(minutes=15),
        status="pending",
    )


def test_approval_proposal_create_parses_json_uuid_strings():
    payload = ApprovalProposalCreate.model_validate(VALID_REQUEST)

    assert isinstance(payload.conversation_id, UUID)
    assert isinstance(payload.source_message_id, UUID)
    assert isinstance(payload.connection_id, UUID)
    assert payload.model_config["strict"] is True


@pytest.mark.parametrize(
    "field",
    ["conversation_id", "source_message_id", "connection_id"],
)
def test_approval_proposal_create_rejects_malformed_uuid_strings(field):
    invalid_request = {**VALID_REQUEST, field: "not-a-uuid"}

    with pytest.raises(ValidationError):
        ApprovalProposalCreate.model_validate(invalid_request)


def test_approval_proposal_create_keeps_unrelated_fields_strict():
    invalid_request = {**VALID_REQUEST, "parameters": "not-a-dictionary"}

    with pytest.raises(ValidationError):
        ApprovalProposalCreate.model_validate(invalid_request)


@pytest.mark.parametrize(
    "field",
    ["conversation_id", "source_message_id", "connection_id"],
)
def test_invalid_approval_uuid_returns_422_before_service_call(
    field,
):
    class ServiceStub:
        calls = []

        def create_proposal(self, **kwargs):
            self.calls.append(kwargs)
            return _proposal(kwargs["user_id"], kwargs["payload"])

    service = ServiceStub()
    app.dependency_overrides[deps.get_current_user] = lambda: {
        "id": str(USER_ID),
    }
    app.dependency_overrides[get_action_approval_service] = lambda: service
    app.dependency_overrides[get_db] = lambda: object()

    try:
        response = TestClient(app).post(
            "/api/v1/action-approvals",
            json={**VALID_REQUEST, field: "not-a-uuid"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
    assert service.calls == []


def test_approval_endpoint_passes_parsed_uuids_to_service():
    class ServiceStub:
        calls = []

        def create_proposal(self, *, user_id, payload):
            self.calls.append((user_id, payload))
            return _proposal(user_id, payload)

    service = ServiceStub()
    app.dependency_overrides[deps.get_current_user] = lambda: {
        "id": str(USER_ID),
    }
    app.dependency_overrides[get_action_approval_service] = lambda: service
    app.dependency_overrides[get_db] = lambda: object()

    try:
        response = TestClient(app).post(
            "/api/v1/action-approvals",
            json=VALID_REQUEST,
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 201
    service_user_id, payload = service.calls[0]
    assert service_user_id == USER_ID
    assert isinstance(payload.conversation_id, UUID)
    assert isinstance(payload.source_message_id, UUID)
    assert isinstance(payload.connection_id, UUID)
    assert response.json()["proposal"]["connection_id"] == str(
        payload.connection_id,
    )


def test_create_proposal_preserves_ownership_checks():
    service = object.__new__(ActionApprovalService)
    payload = ApprovalProposalCreate.model_validate(VALID_REQUEST)
    user_id = uuid4()
    ownership_checks = []
    repository_calls = []

    def validate_connection(*, user_id, connection_id, action_type):
        ownership_checks.append((user_id, connection_id, action_type))
        return SimpleNamespace(email="sender@example.com")

    class RepositoryStub:
        def create_proposal(self, **kwargs):
            repository_calls.append(kwargs)
            return None

    service._validate_connection = validate_connection
    service.repository = RepositoryStub()

    with pytest.raises(LookupError, match="conversation or source"):
        service.create_proposal(user_id=user_id, payload=payload)

    assert ownership_checks == [(
        user_id,
        payload.connection_id,
        payload.action_type,
    )]
    assert repository_calls[0]["user_id"] == user_id
    assert repository_calls[0]["conversation_id"] == payload.conversation_id
    assert repository_calls[0]["source_message_id"] == payload.source_message_id


def test_unowned_connection_is_rejected_before_credentials_are_checked():
    service = object.__new__(ActionApprovalService)
    user_id = uuid4()
    connection_id = uuid4()
    ownership_checks = []

    class ConnectionRepositoryStub:
        def get_active_google_connection(self, *, user_id, connection_id):
            ownership_checks.append((user_id, connection_id))
            return None

    class CredentialRepositoryStub:
        def get_by_connection(self, **_kwargs):
            pytest.fail(
                "Credentials must not be checked for an unowned connection.",
            )

    service.connection_repository = ConnectionRepositoryStub()
    service.credential_repository = CredentialRepositoryStub()

    with pytest.raises(LookupError, match="Connected Google account"):
        service._validate_connection(
            user_id=user_id,
            connection_id=connection_id,
            action_type="gmail.send",
        )

    assert ownership_checks == [(user_id, connection_id)]
