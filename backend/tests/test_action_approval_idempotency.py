from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.api.v1.endpoints import action_approvals
from app.api.v1.endpoints.action_approvals import get_action_approval_service
from app.db.session import get_db
from app.main import app
from app.schemas.action_approval import ActionApprovalProposal
from app.services.action_approval_service import (
    ActionApprovalService,
    ApprovalConflictError,
)


USER_ID = uuid4()
CONNECTION_ID = uuid4()
IDEMPOTENCY_KEY = uuid4()
MESSAGE_ID = "gmail-message-123"
REQUEST_PARAMETERS = {
    "draft_id": "gmail-draft-123",
    "message_id": MESSAGE_ID,
    "to": ["recipient@example.com"],
    "cc": [],
    "bcc": [],
    "subject": "Reviewed subject",
    "body": "Reviewed message",
}


class IdempotentApprovalRepository:
    def __init__(self):
        self.proposals = {}
        self.create_calls = []

    def get_gmail_draft_proposal(self, *, user_id, proposal_id):
        proposal = self.proposals.get(proposal_id)
        if proposal and proposal.user_id == user_id:
            return proposal
        return None

    def create_gmail_draft_proposal(self, **kwargs):
        self.create_calls.append(kwargs)
        existing = self.proposals.get(kwargs["proposal_id"])
        if existing:
            return existing

        now = datetime.now(timezone.utc)
        proposal = ActionApprovalProposal(
            proposal_id=kwargs["proposal_id"],
            message_id=kwargs["proposal_id"],
            source_message_id=kwargs["source_message_id"],
            user_id=kwargs["user_id"],
            conversation_id=kwargs["conversation_id"],
            action_type="gmail.send_draft",
            parameters=kwargs["parameters"],
            connection_id=kwargs["connection_id"],
            account_email=kwargs["account_email"],
            created_at=now,
            expires_at=now + timedelta(minutes=15),
            status="pending",
        )
        self.proposals[proposal.proposal_id] = proposal
        return proposal


def _service():
    service = object.__new__(ActionApprovalService)
    service.repository = IdempotentApprovalRepository()
    service._validate_connection = lambda **_kwargs: SimpleNamespace(
        email="sender@example.com",
    )
    return service


def _create(service, parameters=REQUEST_PARAMETERS):
    return service.create_gmail_draft_proposal(
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        message_id=MESSAGE_ID,
        idempotency_key=IDEMPOTENCY_KEY,
        parameters=parameters,
    )


def test_repeated_proposal_creation_returns_the_same_durable_approval():
    service = _service()

    first = _create(service)
    repeated = _create(service)
    reconciled = service.get_gmail_draft_proposal(
        user_id=USER_ID,
        idempotency_key=IDEMPOTENCY_KEY,
    )

    assert repeated.proposal_id == first.proposal_id
    assert reconciled.proposal_id == first.proposal_id
    assert len(service.repository.proposals) == 1
    assert service.repository.create_calls[0]["proposal_id"] == (
        service.repository.create_calls[1]["proposal_id"]
    )


def test_idempotency_key_cannot_be_reused_for_different_reviewed_content():
    service = _service()
    _create(service)
    changed = {**REQUEST_PARAMETERS, "body": "Different reviewed message"}

    with pytest.raises(ApprovalConflictError, match="different Gmail draft"):
        _create(service, changed)


def _api_proposal(user_id, connection_id, idempotency_key):
    now = datetime.now(timezone.utc)
    proposal_id = ActionApprovalService._gmail_draft_proposal_id(
        user_id,
        idempotency_key,
    )
    return ActionApprovalProposal(
        proposal_id=proposal_id,
        message_id=uuid4(),
        source_message_id=uuid4(),
        user_id=user_id,
        conversation_id=uuid4(),
        action_type="gmail.send_draft",
        parameters=REQUEST_PARAMETERS,
        connection_id=connection_id,
        account_email="sender@example.com",
        created_at=now,
        expires_at=now + timedelta(minutes=15),
        status="pending",
    )


def test_approval_endpoint_reuses_key_and_exposes_a_status_check(
    monkeypatch,
):
    user_id = uuid4()
    idempotency_key = uuid4()
    connection_id = uuid4()
    proposal = _api_proposal(user_id, connection_id, idempotency_key)

    class ServiceStub:
        current = None
        current_key = None
        create_calls = []
        match_calls = []

        def get_gmail_draft_proposal(self, *, user_id, idempotency_key):
            return self.current if self.current_key == idempotency_key else None

        def validate_gmail_draft_request_match(self, **kwargs):
            self.match_calls.append(kwargs)

        def create_gmail_draft_proposal(self, **kwargs):
            self.create_calls.append(kwargs)
            self.current = proposal
            self.current_key = kwargs["idempotency_key"]
            return proposal

    class GmailStub:
        calls = []

        def __init__(self, _db):
            pass

        async def get_draft_id_by_message_id(self, **kwargs):
            self.calls.append(kwargs)
            return REQUEST_PARAMETERS["draft_id"]

    service = ServiceStub()
    monkeypatch.setattr(action_approvals, "GmailService", GmailStub)
    app.dependency_overrides[deps.get_current_user] = lambda: {
        "id": str(user_id),
    }
    app.dependency_overrides[get_action_approval_service] = lambda: service
    app.dependency_overrides[get_db] = lambda: object()

    try:
        client = TestClient(app)
        path = f"/api/v1/action-approvals/gmail-drafts/{MESSAGE_ID}"
        payload = {
            "connection_id": str(connection_id),
            "to": REQUEST_PARAMETERS["to"],
            "cc": [],
            "bcc": [],
            "subject": REQUEST_PARAMETERS["subject"],
            "body": REQUEST_PARAMETERS["body"],
        }
        headers = {
            "Authorization": "Bearer test-token",
            "Origin": "https://www.myorbitai.in",
            "X-Request-ID": str(uuid4()),
            "Idempotency-Key": str(idempotency_key),
        }

        first = client.post(path, json=payload, headers=headers)
        assert first.status_code == 201
        assert first.headers.get("x-request-id") == headers["X-Request-ID"]
        assert first.json()["proposal"]["proposal_id"] == str(
            proposal.proposal_id,
        )
        assert len(GmailStub.calls) == 1
        assert service.create_calls[0]["idempotency_key"] == idempotency_key

        repeated = client.post(path, json=payload, headers=headers)
        assert repeated.status_code == 201
        assert repeated.json()["proposal"]["proposal_id"] == str(
            proposal.proposal_id,
        )
        assert len(GmailStub.calls) == 1

        invalid_payload = {**payload, "subject": "x" * 501}
        invalid = client.post(
            path,
            json=invalid_payload,
            headers={**headers, "Idempotency-Key": str(uuid4())},
        )
        assert invalid.status_code == 422
        assert len(GmailStub.calls) == 1

        status_response = client.get(
            "/api/v1/action-approvals/gmail-drafts/approval/"
            f"{idempotency_key}",
            headers=headers,
        )
        assert status_response.status_code == 200
        assert status_response.json()["proposal_id"] == str(
            proposal.proposal_id,
        )
        missing = client.get(
            "/api/v1/action-approvals/gmail-drafts/approval/"
            f"{uuid4()}",
            headers=headers,
        )
        assert missing.status_code == 404
    finally:
        app.dependency_overrides.clear()
