import asyncio
import base64
from datetime import datetime, timezone
from email import message_from_bytes
from email.policy import default
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.v1.endpoints import messages as messages_endpoint
from app.integrations.google.exceptions import GoogleAPIError
from app.integrations.google.message_builder import GmailMessageBuilder
from app.schemas.email_draft import EmailDraftPreview, GmailDraftSaveRequest
from app.services.gmail_service import GmailService


USER_ID = uuid4()
CONVERSATION_ID = uuid4()
MESSAGE_ID = uuid4()
CONNECTION_ID = uuid4()
OPERATION_ID = uuid4()
DRAFT = EmailDraftPreview(
    draft_type="new",
    to=[{"email": "subrata291291@gmail.com"}],
    cc=[{"email": "copy@example.com"}],
    bcc=[{"email": "blind@example.com"}],
    subject="Draft subject",
    body="Draft body",
)


def test_create_draft_uses_operation_marker_and_never_sends(monkeypatch):
    service = GmailService(db=None)
    calls = []

    async def fake_google_post(**kwargs):
        calls.append(kwargs)
        return {
            "id": "draft-1",
            "message": {"id": "message-1", "threadId": "thread-1"},
        }

    monkeypatch.setattr(service, "_validate_connection", lambda **_kwargs: None)
    monkeypatch.setattr(service, "_google_post", fake_google_post)

    result = asyncio.run(
        service.create_draft(
            user_id=USER_ID,
            connection_id=CONNECTION_ID,
            draft=DRAFT,
            operation_id=OPERATION_ID,
        )
    )

    assert result["draft_id"] == "draft-1"
    assert len(calls) == 1
    assert calls[0]["endpoint"] == "drafts"
    assert calls[0]["endpoint"] != "messages/send"
    raw = calls[0]["json_data"]["message"]["raw"]
    decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    parsed = message_from_bytes(decoded, policy=default)
    assert parsed["To"] == "subrata291291@gmail.com"
    assert parsed["Cc"] == "copy@example.com"
    assert parsed["Bcc"] == "blind@example.com"
    assert parsed["Subject"] == "Draft subject"
    assert parsed["X-Personal-AI-Draft-Operation-ID"] == str(OPERATION_ID)


def test_operation_marker_finds_existing_gmail_draft(monkeypatch):
    service = GmailService(db=None)
    requests = []

    async def fake_google_get(**kwargs):
        requests.append(kwargs)
        if kwargs["endpoint"] == "drafts":
            return {"drafts": [{"id": "draft-existing"}]}
        return {
            "message": {
                "id": "gmail-message-existing",
                "threadId": "thread-existing",
                "payload": {
                    "headers": [{
                        "name": "X-Personal-AI-Draft-Operation-ID",
                        "value": str(OPERATION_ID),
                    }],
                },
            },
        }

    monkeypatch.setattr(service, "_validate_connection", lambda **_kwargs: None)
    monkeypatch.setattr(service, "_google_get", fake_google_get)

    result = asyncio.run(
        service.find_draft_by_operation_id(
            user_id=USER_ID,
            connection_id=CONNECTION_ID,
            operation_id=OPERATION_ID,
        )
    )

    assert result == {
        "draft": {
            "draft_id": "draft-existing",
            "message_id": "gmail-message-existing",
            "thread_id": "thread-existing",
        },
        "complete_scan": True,
    }
    assert len(requests) == 2
    assert requests[1]["endpoint"] == "drafts/draft-existing"


def test_recipient_validation_allows_empty_draft_but_rejects_invalid_address():
    empty_recipient_draft = EmailDraftPreview(
        draft_type="new",
        to=[],
        subject="No recipient yet",
        body="A Gmail draft can be saved before adding a recipient.",
    )
    raw = GmailMessageBuilder.build(
        to=[],
        subject=empty_recipient_draft.subject,
        body=empty_recipient_draft.body,
        require_recipient=False,
    )
    decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    parsed = message_from_bytes(decoded, policy=default)
    assert parsed["To"] is None

    with pytest.raises(ValidationError, match="recipient email address is invalid"):
        EmailDraftPreview(
            draft_type="new",
            to=[{"email": "not-an-email"}],
            subject="Invalid recipient",
            body="This should not be sent to Gmail.",
        )


class DraftSaveStore:
    def __init__(self, state="saving"):
        self.state = state
        self.record = {
            "state": state,
            "operation_id": str(OPERATION_ID),
            "connection_id": str(CONNECTION_ID),
            "fingerprint": "fingerprint",
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        self.finish_calls = []
        self.create_claim = {
            "status": "claimed",
            "record": self.record,
        }

    def get_email_draft_for_gmail_save(self, **_kwargs):
        return ({"id": str(MESSAGE_ID)}, DRAFT)

    @staticmethod
    def email_draft_fingerprint(_draft):
        return "fingerprint"

    def claim_gmail_draft_save(self, **_kwargs):
        return self.create_claim

    def finish_gmail_draft_save(self, **kwargs):
        self.finish_calls.append(kwargs)
        self.state = kwargs["state"]
        self.record.update(state=self.state)
        if kwargs.get("result"):
            self.record["saved_result"] = kwargs["result"]
            self.record["saved_preview"] = kwargs.get("saved_preview")
            self.record["saved_fingerprint"] = "fingerprint"
        return {"id": str(MESSAGE_ID)}

    def get_gmail_draft_save_record(self, **_kwargs):
        return {"status": self.state, "record": self.record}


class DraftGmail:
    def __init__(self, create_result=None, create_error=None, found=None):
        self.create_result = create_result or {
            "draft_id": "draft-1",
            "message_id": "gmail-message-1",
            "thread_id": None,
        }
        self.create_error = create_error
        self.found = found
        self.create_calls = []
        self.find_calls = []

    def validate_draft_access(self, **_kwargs):
        return None

    async def create_draft(self, **kwargs):
        self.create_calls.append(kwargs)
        if self.create_error:
            raise self.create_error
        return self.create_result

    async def find_draft_by_operation_id(self, **kwargs):
        self.find_calls.append(kwargs)
        return {"draft": self.found, "complete_scan": True}


def test_created_draft_after_timeout_is_reconciled_without_duplicate(monkeypatch):
    store = DraftSaveStore()
    existing = {
        "draft_id": "draft-created-before-timeout",
        "message_id": "gmail-message-1",
        "thread_id": "thread-1",
    }
    gmail = DraftGmail(
        create_error=GoogleAPIError(503, "timeout", retryable=False),
        found=existing,
    )
    monkeypatch.setattr(messages_endpoint, "GmailService", lambda _db: gmail)
    payload = GmailDraftSaveRequest(
        connection_id=CONNECTION_ID,
        operation_id=OPERATION_ID,
    )

    with pytest.raises(HTTPException) as save_error:
        asyncio.run(
            messages_endpoint.save_email_draft_to_gmail(
                CONVERSATION_ID,
                MESSAGE_ID,
                payload,
                {"id": str(USER_ID)},
                store,
                db=None,
            )
        )
    assert save_error.value.status_code == 409
    assert store.state == "uncertain"
    assert store.record["operation_id"] == str(OPERATION_ID)

    result = asyncio.run(
        messages_endpoint.get_email_draft_save_status(
            CONVERSATION_ID,
            MESSAGE_ID,
            CONNECTION_ID,
            OPERATION_ID,
            {"id": str(USER_ID)},
            store,
            db=None,
        )
    )

    assert result.state == "saved"
    assert result.draft_id == existing["draft_id"]
    assert result.operation_id == OPERATION_ID
    assert gmail.find_calls[0]["operation_id"] == OPERATION_ID
    assert len(gmail.create_calls) == 1
    assert store.record["state"] == "saved"


def test_pending_operation_does_not_scan_or_create_another_draft(monkeypatch):
    store = DraftSaveStore()
    gmail = DraftGmail()
    monkeypatch.setattr(messages_endpoint, "GmailService", lambda _db: gmail)

    result = asyncio.run(
        messages_endpoint.get_email_draft_save_status(
            CONVERSATION_ID,
            MESSAGE_ID,
            CONNECTION_ID,
            OPERATION_ID,
            {"id": str(USER_ID)},
            store,
            db=None,
        )
    )

    assert result.state == "saving"
    assert gmail.find_calls == []
    assert gmail.create_calls == []


def test_definitive_failure_allows_retry_using_same_operation_id(monkeypatch):
    store = DraftSaveStore()
    gmail = DraftGmail(
        create_error=GoogleAPIError(403, "permission denied", retryable=False),
    )
    monkeypatch.setattr(messages_endpoint, "GmailService", lambda _db: gmail)
    store.create_claim = {
        "status": "claimed",
        "record": store.record,
    }

    with pytest.raises(HTTPException) as save_error:
        asyncio.run(
            messages_endpoint.save_email_draft_to_gmail(
                CONVERSATION_ID,
                MESSAGE_ID,
                GmailDraftSaveRequest(
                    connection_id=CONNECTION_ID,
                    operation_id=OPERATION_ID,
                ),
                {"id": str(USER_ID)},
                store,
                db=None,
            )
        )
    assert save_error.value.status_code == 403
    assert store.state == "failed"

    failed_status = asyncio.run(
        messages_endpoint.get_email_draft_save_status(
            CONVERSATION_ID,
            MESSAGE_ID,
            CONNECTION_ID,
            OPERATION_ID,
            {"id": str(USER_ID)},
            store,
            db=None,
        )
    )
    assert failed_status.state == "failed"
    assert "safe to retry" in failed_status.detail
    assert gmail.find_calls == []

    gmail.create_error = None
    result = asyncio.run(
        messages_endpoint.save_email_draft_to_gmail(
            CONVERSATION_ID,
            MESSAGE_ID,
            GmailDraftSaveRequest(
                connection_id=CONNECTION_ID,
                operation_id=OPERATION_ID,
            ),
            {"id": str(USER_ID)},
            store,
            db=None,
        )
    )

    assert result.state == "saved"
    assert gmail.create_calls[0]["operation_id"] == OPERATION_ID
