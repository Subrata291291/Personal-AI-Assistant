from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.api.v1.endpoints import action_approvals
from app.api.v1.endpoints.action_approvals import get_action_approval_service
from app.db.session import get_db
from app.main import app
from app.schemas.action_approval import ActionApprovalProposal
from app.services.action_approval_service import ActionApprovalService


MESSAGE_ID = "gmail-message-123"
IDEMPOTENCY_KEY = uuid4()
CONNECTION_ID = uuid4()
REQUEST = {
    "connection_id": str(CONNECTION_ID),
    "to": ["recipient@example.com"],
    "cc": [],
    "bcc": [],
    "subject": "Reviewed subject",
    "body": "Reviewed message",
}


def _proposal(user_id, idempotency_key):
    now = datetime.now(timezone.utc)
    return ActionApprovalProposal(
        proposal_id=ActionApprovalService._gmail_draft_proposal_id(
            user_id,
            idempotency_key,
        ),
        message_id=uuid4(),
        source_message_id=uuid4(),
        user_id=user_id,
        conversation_id=uuid4(),
        action_type="gmail.send_draft",
        parameters={
            "draft_id": "gmail-draft-123",
            "message_id": MESSAGE_ID,
            "to": REQUEST["to"],
            "cc": REQUEST["cc"],
            "bcc": REQUEST["bcc"],
            "subject": REQUEST["subject"],
            "body": REQUEST["body"],
        },
        connection_id=CONNECTION_ID,
        account_email="sender@example.com",
        created_at=now,
        expires_at=now + timedelta(minutes=15),
        status="pending",
    )


class DraftGmailStub:
    lookups = []
    sends = []

    def __init__(self, _db):
        pass

    async def get_draft_id_by_message_id(self, **kwargs):
        self.lookups.append(kwargs)
        return "gmail-draft-123"


@pytest.fixture
def api_client(monkeypatch):
    user_id = uuid4()
    app.dependency_overrides[deps.get_current_user] = lambda: {
        "id": str(user_id),
    }
    app.dependency_overrides[get_db] = lambda: object()
    monkeypatch.setattr(action_approvals, "GmailService", DraftGmailStub)
    DraftGmailStub.lookups = []
    DraftGmailStub.sends = []
    try:
        yield TestClient(app), user_id
    finally:
        app.dependency_overrides.clear()


def _headers(idempotency_key=IDEMPOTENCY_KEY):
    return {
        "Idempotency-Key": str(idempotency_key),
        "X-Request-ID": str(uuid4()),
    }


def _post(client, payload=REQUEST, idempotency_key=IDEMPOTENCY_KEY):
    return client.post(
        f"/api/v1/action-approvals/gmail-drafts/{MESSAGE_ID}",
        json=payload,
        headers=_headers(idempotency_key),
    )


def _get_status(client, idempotency_key=IDEMPOTENCY_KEY):
    return client.get(
        "/api/v1/action-approvals/gmail-drafts/approval/"
        f"{idempotency_key}",
        headers=_headers(idempotency_key),
    )


def test_committed_approval_is_reconciled_after_creation_confirmation_fails(
    api_client,
):
    client, user_id = api_client

    class DurableService:
        proposals = {}
        create_calls = 0

        def get_gmail_draft_proposal(self, *, user_id, idempotency_key):
            return self.proposals.get(
                ActionApprovalService._gmail_draft_proposal_id(
                    user_id,
                    idempotency_key,
                ),
            )

        def create_gmail_draft_proposal(self, **kwargs):
            self.create_calls += 1
            proposal = _proposal(kwargs["user_id"], kwargs["idempotency_key"])
            self.proposals[proposal.proposal_id] = proposal
            raise RuntimeError("Simulated lost creation confirmation")

        def validate_gmail_draft_request_match(self, **_kwargs):
            return None

    service = DurableService()
    app.dependency_overrides[get_action_approval_service] = lambda: service

    response = _post(client)
    assert response.status_code == 503
    assert len(service.proposals) == 1

    reconciled = _get_status(client)
    assert reconciled.status_code == 200
    proposal_id = reconciled.json()["proposal_id"]
    assert proposal_id == str(
        ActionApprovalService._gmail_draft_proposal_id(
            user_id,
            IDEMPOTENCY_KEY,
        ),
    )
    assert DraftGmailStub.sends == []

    repeated = _post(client)
    assert repeated.status_code == 201
    assert repeated.json()["proposal"]["proposal_id"] == proposal_id
    assert service.create_calls == 1
    assert len(service.proposals) == 1
    assert len(DraftGmailStub.lookups) == 1
    assert DraftGmailStub.sends == []


def test_failed_persistence_and_missing_or_mismatched_status_stay_distinct(
    api_client,
):
    client, _user_id = api_client

    class FailingService:
        def __init__(self):
            self.create_calls = 0

        def get_gmail_draft_proposal(self, **_kwargs):
            return None

        def create_gmail_draft_proposal(self, **_kwargs):
            self.create_calls += 1
            raise RuntimeError("Simulated database persistence failure")

    service = FailingService()
    app.dependency_overrides[get_action_approval_service] = lambda: service

    failed = _post(client)
    assert failed.status_code == 503
    assert "Approval creation could not be confirmed" in failed.json()["detail"]

    missing = _get_status(client)
    assert missing.status_code == 404

    mismatched = _get_status(client, uuid4())
    assert mismatched.status_code == 404
    assert service.create_calls == 1
    assert DraftGmailStub.sends == []


def test_status_lookup_failure_returns_503_not_missing_record(api_client):
    client, _user_id = api_client

    class LookupFailureService:
        def get_gmail_draft_proposal(self, **_kwargs):
            raise RuntimeError("Simulated status storage failure")

    app.dependency_overrides[get_action_approval_service] = (
        lambda: LookupFailureService()
    )

    response = _get_status(client)

    assert response.status_code == 503
    assert "status could not be checked" in response.json()["detail"]


def test_status_lookup_is_scoped_to_authenticated_user(api_client):
    client, user_id = api_client
    idempotency_key = uuid4()
    proposal = _proposal(user_id, idempotency_key)

    class UserScopedService:
        def get_gmail_draft_proposal(self, *, user_id, idempotency_key):
            expected_id = ActionApprovalService._gmail_draft_proposal_id(
                user_id,
                idempotency_key,
            )
            return proposal if proposal.proposal_id == expected_id else None

    app.dependency_overrides[get_action_approval_service] = (
        lambda: UserScopedService()
    )

    assert _get_status(client, idempotency_key).status_code == 200

    other_user_id = uuid4()
    app.dependency_overrides[deps.get_current_user] = lambda: {
        "id": str(other_user_id),
    }
    assert _get_status(client, idempotency_key).status_code == 404


def test_calendar_status_get_returns_reconciled_proposal_without_execution(
    api_client,
    monkeypatch,
):
    client, user_id = api_client
    owner_id = user_id
    proposal_id = uuid4()
    now = datetime.now(timezone.utc)
    proposal = ActionApprovalProposal.model_validate({
        "proposal_id": proposal_id,
        "message_id": uuid4(),
        "source_message_id": uuid4(),
        "user_id": user_id,
        "conversation_id": uuid4(),
        "action_type": "calendar.delete_event",
        "parameters": {
            "calendar_id": "primary",
            "event_id": "series-master",
            "expected_etag": '"etag-1"',
            "current_event": {
                "summary": "Gym",
                "start": {
                    "dateTime": "2026-11-11T10:00:00+05:30",
                    "timeZone": "Asia/Kolkata",
                },
                "end": {
                    "dateTime": "2026-11-11T11:00:00+05:30",
                    "timeZone": "Asia/Kolkata",
                },
                "recurrence": ["RRULE:FREQ=DAILY"],
                "etag": '"etag-1"',
            },
            "delete_entire_series": True,
        },
        "connection_id": CONNECTION_ID,
        "created_at": now,
        "expires_at": now + timedelta(minutes=15),
        "status": "execution_uncertain",
        "result": {
            "outcome": "unconfirmed",
            "reason": "event_still_exists",
        },
    })
    reconciled = proposal.model_copy(update={
        "status": "completed",
        "result": {
            "outcome": "deleted",
            "google_event_id": "series-master",
        },
    })

    class CalendarStatusService:
        def get_proposal(self, *, user_id, proposal_id):
            assert user_id == owner_id
            assert proposal_id == proposal.proposal_id
            return proposal

    class CalendarExecutorStub:
        SUPPORTED_ACTION_TYPES = {"calendar.delete_event"}
        instances = []

        def __init__(self, *, db, approval_service):
            self.db = db
            self.approval_service = approval_service
            self.reconcile_calls = []
            self.instances.append(self)

        async def reconcile(self, *, user_id, proposal_id):
            self.reconcile_calls.append((user_id, proposal_id))
            return reconciled

    service = CalendarStatusService()
    CalendarExecutorStub.instances = []
    app.dependency_overrides[get_action_approval_service] = lambda: service
    monkeypatch.setattr(action_approvals, "CalendarActionExecutor", CalendarExecutorStub)
    monkeypatch.setattr(action_approvals, "is_integration_enabled", lambda *_: True)

    response = client.get(
        f"/api/v1/action-approvals/{proposal_id}",
        headers=_headers(),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert response.json()["result"]["outcome"] == "deleted"
    assert CalendarExecutorStub.instances[0].reconcile_calls == [
        (user_id, proposal_id),
    ]
