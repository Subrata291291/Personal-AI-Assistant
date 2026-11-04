import asyncio
import base64
from datetime import datetime, timedelta, timezone
from email import message_from_bytes
from email.policy import default
from uuid import uuid4

import pytest

from app.integrations.google.exceptions import GoogleAPIError
from app.schemas.action_approval import ActionApprovalProposal
from app.services.action_approval_service import ApprovalConflictError
from app.services.gmail_action_executor import GmailActionExecutor
from app.services.gmail_service import GmailService


USER_ID = uuid4()
CONNECTION_ID = uuid4()
PROPOSAL_ID = uuid4()
MESSAGE_ID = "gmail-message-123"
DRAFT_ID = "gmail-draft-123"
PARAMETERS = {
    "draft_id": DRAFT_ID,
    "message_id": MESSAGE_ID,
    "to": ["recipient@example.com"],
    "cc": [],
    "bcc": [],
    "subject": "Draft subject",
    "body": "Draft body",
}


def _proposal(status="approved"):
    now = datetime.now(timezone.utc)
    return ActionApprovalProposal(
        proposal_id=PROPOSAL_ID,
        message_id=uuid4(),
        source_message_id=uuid4(),
        user_id=USER_ID,
        conversation_id=uuid4(),
        action_type="gmail.send_draft",
        parameters=PARAMETERS,
        connection_id=CONNECTION_ID,
        account_email="sender@example.com",
        created_at=now,
        expires_at=now + timedelta(minutes=15),
        status=status,
        approved_at=now,
        execution_started_at=now if status in {
            "executing",
            "execution_uncertain",
        } else None,
    )


def test_draft_lookup_paginates_and_confirms_draft_message(monkeypatch):
    service = object.__new__(GmailService)
    calls = []

    async def fake_google_get(**kwargs):
        calls.append(kwargs)
        if kwargs["endpoint"] == "drafts":
            if kwargs["params"].get("pageToken") is None:
                return {
                    "drafts": [{"id": "other-draft", "message": {"id": "other"}}],
                    "nextPageToken": "next-page",
                }
            return {
                "drafts": [{
                    "id": DRAFT_ID,
                    "message": {"id": MESSAGE_ID},
                }],
            }
        return {
            "message": {
                "id": MESSAGE_ID,
                "labelIds": ["DRAFT"],
            },
        }

    monkeypatch.setattr(service, "_validate_connection", lambda **_kwargs: None)
    monkeypatch.setattr(service, "_google_get", fake_google_get)

    draft_id = asyncio.run(service.get_draft_id_by_message_id(
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        message_id=MESSAGE_ID,
    ))

    assert draft_id == DRAFT_ID
    assert calls[1]["params"]["pageToken"] == "next-page"
    assert calls[2]["endpoint"] == f"drafts/{DRAFT_ID}"


def test_draft_with_attachment_is_rejected_before_it_can_be_replaced(monkeypatch):
    service = object.__new__(GmailService)
    calls = []

    async def fake_google_get(**kwargs):
        if kwargs["endpoint"] == "drafts":
            return {
                "drafts": [{
                    "id": DRAFT_ID,
                    "message": {"id": MESSAGE_ID},
                }],
            }
        return {
            "message": {
                "id": MESSAGE_ID,
                "labelIds": ["DRAFT"],
                "payload": {
                    "parts": [{
                        "filename": "proposal.pdf",
                        "body": {"attachmentId": "attachment-1"},
                    }],
                },
            },
        }

    async def fake_google_put(**kwargs):
        calls.append(("put", kwargs))

    async def fake_google_post(**kwargs):
        calls.append(("post", kwargs))

    monkeypatch.setattr(service, "_validate_connection", lambda **_kwargs: None)
    monkeypatch.setattr(service, "_google_get", fake_google_get)
    monkeypatch.setattr(service, "_google_put", fake_google_put)
    monkeypatch.setattr(service, "_google_post", fake_google_post)

    with pytest.raises(ValueError, match="attachments"):
        asyncio.run(service.get_draft_id_by_message_id(
            user_id=USER_ID,
            connection_id=CONNECTION_ID,
            message_id=MESSAGE_ID,
        ))

    with pytest.raises(ValueError, match="attachments"):
        asyncio.run(service.send_draft(
            user_id=USER_ID,
            connection_id=CONNECTION_ID,
            **PARAMETERS,
            operation_id=PROPOSAL_ID,
        ))
    assert calls == []


def test_approved_draft_updates_then_sends_with_operation_marker(monkeypatch):
    service = object.__new__(GmailService)
    calls = []

    async def fake_google_get(**_kwargs):
        return {
            "message": {
                "id": MESSAGE_ID,
                "labelIds": ["DRAFT"],
            },
        }

    async def fake_google_put(**kwargs):
        calls.append(("put", kwargs))
        return {}

    async def fake_google_post(**kwargs):
        calls.append(("post", kwargs))
        return {
            "id": "sent-message-456",
            "threadId": "sent-thread-456",
            "labelIds": ["SENT"],
        }

    monkeypatch.setattr(service, "_validate_connection", lambda **_kwargs: None)
    monkeypatch.setattr(service, "_google_get", fake_google_get)
    monkeypatch.setattr(service, "_google_put", fake_google_put)
    monkeypatch.setattr(service, "_google_post", fake_google_post)

    result = asyncio.run(service.send_draft(
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        **PARAMETERS,
        operation_id=PROPOSAL_ID,
    ))

    updated_raw = calls[0][1]["json_data"]["message"]["raw"]
    decoded = base64.urlsafe_b64decode(
        updated_raw + "=" * (-len(updated_raw) % 4)
    )
    updated_message = message_from_bytes(decoded, policy=default)
    assert updated_message["X-Personal-AI-Draft-Operation-ID"] == str(
        PROPOSAL_ID
    )
    assert [call[0] for call in calls] == ["put", "post"]
    assert calls[1][1]["endpoint"] == "drafts/send"
    assert calls[1][1]["json_data"] == {"id": DRAFT_ID}
    assert result["id"] == "sent-message-456"


class ApprovalServiceStub:
    def __init__(self, proposal):
        self.proposal = proposal
        self.finish_calls = []

    def get_proposal(self, **_kwargs):
        return self.proposal

    def claim_execution(self, **_kwargs):
        if self.proposal.status != "approved":
            raise ApprovalConflictError("Proposal is not approved.")
        self.proposal = self.proposal.model_copy(
            update={
                "status": "executing",
                "execution_started_at": datetime.now(timezone.utc),
            },
        )
        return self.proposal

    def finish_execution(self, *, state, safe_result, **_kwargs):
        self.finish_calls.append((state, safe_result))
        self.proposal = self.proposal.model_copy(
            update={
                "status": state,
                "result": safe_result,
                "completed_at": datetime.now(timezone.utc),
            },
        )
        return self.proposal

    def finish_reconciled_execution(self, *, safe_result, **_kwargs):
        self.proposal = self.proposal.model_copy(
            update={
                "status": "completed",
                "result": safe_result,
                "completed_at": datetime.now(timezone.utc),
            },
        )
        return self.proposal


class GmailDraftServiceStub:
    def __init__(self, result=None, error=None, reconciliation=None):
        self.result = result
        self.error = error
        self.reconciliation = reconciliation
        self.send_calls = []
        self.reconcile_calls = []

    async def send_draft(self, **kwargs):
        self.send_calls.append(kwargs)
        if self.error:
            raise self.error
        return self.result

    async def find_sent_draft_by_operation_id(self, **kwargs):
        self.reconcile_calls.append(kwargs)
        return self.reconciliation


def test_executor_requires_approved_proposal_and_sends_only_once():
    approval = ApprovalServiceStub(_proposal())
    gmail = GmailDraftServiceStub(
        result={"id": "sent-message-456", "thread_id": "thread-456"},
    )
    executor = GmailActionExecutor(
        db=None,
        approval_service=approval,
        gmail_service=gmail,
    )

    result = asyncio.run(executor.execute(USER_ID, PROPOSAL_ID))

    assert result.status == "completed"
    assert gmail.send_calls[0]["operation_id"] == PROPOSAL_ID
    assert approval.finish_calls[0][0] == "completed"
    with pytest.raises(ApprovalConflictError):
        asyncio.run(executor.execute(USER_ID, PROPOSAL_ID))
    assert len(gmail.send_calls) == 1


def test_uncertain_draft_send_is_reconciled_without_retrying_send():
    proposal = _proposal("approved")
    approval = ApprovalServiceStub(proposal)
    gmail = GmailDraftServiceStub(
        error=GoogleAPIError(500, "Temporary provider failure"),
    )
    executor = GmailActionExecutor(
        db=None,
        approval_service=approval,
        gmail_service=gmail,
    )

    uncertain = asyncio.run(executor.execute(USER_ID, PROPOSAL_ID))
    assert uncertain.status == "execution_uncertain"
    assert len(gmail.send_calls) == 1

    gmail.reconciliation = None
    unresolved = asyncio.run(executor.reconcile(USER_ID, PROPOSAL_ID))
    assert unresolved.status == "execution_uncertain"
    assert len(gmail.send_calls) == 1
    assert len(gmail.reconcile_calls) == 1

    gmail.reconciliation = {"id": "sent-message-456", "thread_id": "thread-456"}
    completed = asyncio.run(executor.reconcile(USER_ID, PROPOSAL_ID))
    assert completed.status == "completed"
    assert completed.result["gmail_message_id"] == "sent-message-456"
    assert len(gmail.send_calls) == 1


def test_definitive_gmail_rejection_does_not_retry_the_send():
    approval = ApprovalServiceStub(_proposal())
    gmail = GmailDraftServiceStub(
        error=GoogleAPIError(400, "Invalid request"),
    )
    executor = GmailActionExecutor(
        db=None,
        approval_service=approval,
        gmail_service=gmail,
    )

    failed = asyncio.run(executor.execute(USER_ID, PROPOSAL_ID))

    assert failed.status == "failed"
    assert approval.finish_calls[0][0] == "failed"
    assert len(gmail.send_calls) == 1
    with pytest.raises(ApprovalConflictError):
        asyncio.run(executor.execute(USER_ID, PROPOSAL_ID))
    assert len(gmail.send_calls) == 1
