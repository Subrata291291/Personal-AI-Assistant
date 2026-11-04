import asyncio
import base64
from datetime import datetime, timezone
from email import message_from_bytes
from email.policy import default
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.deps import get_current_user
from app.api.v1.endpoints import messages as messages_endpoint
from app.db.session import get_db
from app.integrations.google.api_client import GoogleAPIClient
from app.integrations.google.exceptions import GoogleAPIError
from app.integrations.google.message_builder import GmailMessageBuilder
from app.schemas.email_draft import EmailDraftPreview, GmailDraftSaveRequest
from app.services.gmail_service import GmailService
from app.services.message_service import MessageService


USER_ID = uuid4()
CONVERSATION_ID = uuid4()
MESSAGE_ID = uuid4()
CONNECTION_ID = uuid4()
OPERATION_ID = uuid4()
VALID_CONNECTION_ID = "4e703667-ef83-49e5-9fb1-99c89bf2b136"
VALID_OPERATION_ID = "5f582828-884a-4f2f-a3ab-dd10440e255d"
DRAFT = EmailDraftPreview(
    draft_type="new",
    to=[{"email": "subrata291291@gmail.com"}],
    cc=[{"email": "copy@example.com"}],
    bcc=[{"email": "blind@example.com"}],
    subject="Draft subject",
    body="Draft body",
)


def test_gmail_draft_save_request_parses_json_uuid_strings():
    request = GmailDraftSaveRequest.model_validate({
        "connection_id": VALID_CONNECTION_ID,
        "operation_id": VALID_OPERATION_ID,
    })

    assert request.connection_id == UUID(VALID_CONNECTION_ID)
    assert request.operation_id == UUID(VALID_OPERATION_ID)
    assert isinstance(request.connection_id, UUID)
    assert isinstance(request.operation_id, UUID)


@pytest.mark.parametrize(
    "field",
    ["connection_id", "operation_id"],
)
def test_gmail_draft_save_request_rejects_malformed_uuid_strings(field):
    payload = {
        "connection_id": VALID_CONNECTION_ID,
        "operation_id": VALID_OPERATION_ID,
    }
    payload[field] = "not-a-uuid"

    with pytest.raises(ValidationError):
        GmailDraftSaveRequest.model_validate(payload)


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


def test_operation_marker_scan_reaches_pages_beyond_five_hundred(monkeypatch):
    service = GmailService(db=None)
    pages = []
    detail_requests = []
    expected_page_tokens = [f"page-{number}" for number in range(2, 7)]

    async def fake_google_get(**kwargs):
        if kwargs["endpoint"] == "drafts":
            pages.append(kwargs["params"])
            page_number = len(pages)
            page = {
                "drafts": [
                    {"id": f"draft-{page_number}-{index}"}
                    for index in range(100)
                ],
            }
            if page_number <= 5:
                page["nextPageToken"] = expected_page_tokens[page_number - 1]
            else:
                page["drafts"] = [{"id": "draft-page-six"}]
            return page

        detail_requests.append(kwargs["endpoint"])
        message_headers = (
            [{
                "name": "x-personal-ai-draft-operation-id",
                "value": str(OPERATION_ID),
            }]
            if kwargs["endpoint"] == "drafts/draft-page-six"
            else []
        )
        return {
            "message": {
                "id": "message-page-six",
                "threadId": "thread-page-six",
                "payload": {"headers": message_headers},
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

    assert result["draft"]["draft_id"] == "draft-page-six"
    assert result["complete_scan"] is True
    assert len(pages) == 6
    assert pages[-1]["pageToken"] == "page-6"
    assert detail_requests[-1] == "drafts/draft-page-six"


def test_missing_operation_marker_is_not_a_confirmed_absence(monkeypatch):
    service = GmailService(db=None)

    async def fake_google_get(**kwargs):
        if kwargs["endpoint"] == "drafts":
            return {"drafts": [{"id": "draft-without-marker"}]}
        return {
            "message": {
                "id": "message-without-marker",
                "payload": {
                    "headers": [{"name": "Subject", "value": "No marker"}],
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

    assert result == {"draft": None, "complete_scan": True}


def test_google_api_error_reason_is_extracted_without_error_message():
    response = httpx.Response(
        403,
        json={
            "error": {
                "status": "PERMISSION_DENIED",
                "message": "Sensitive response text",
                "errors": [{
                    "reason": "insufficientPermissions",
                    "message": "Sensitive detail",
                }],
            },
        },
    )

    assert GoogleAPIClient._error_reason(response) == "PERMISSION_DENIED"


def test_google_api_client_does_not_retry_timed_out_draft_post():
    client = GoogleAPIClient()
    request_count = 0

    async def timeout_request(_request):
        nonlocal request_count
        request_count += 1
        raise httpx.ReadTimeout("request timeout")

    async def run_request():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(timeout_request),
        ) as http_client:
            await client._send_request(
                client=http_client,
                method="POST",
                url="https://gmail.googleapis.com/gmail/v1/users/me/drafts",
                headers={},
                params=None,
                json_data={"message": {"raw": "payload"}},
            )

    with pytest.raises(GoogleAPIError) as request_error:
        asyncio.run(run_request())

    assert request_error.value.status_code == 503
    assert request_count == 1


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
    def __init__(
        self,
        create_result=None,
        create_error=None,
        found=None,
        complete_scan=True,
    ):
        self.create_result = create_result or {
            "draft_id": "draft-1",
            "message_id": "gmail-message-1",
            "thread_id": None,
        }
        self.create_error = create_error
        self.found = found
        self.complete_scan = complete_scan
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
        return {
            "draft": self.found,
            "complete_scan": self.complete_scan,
        }


def draft_save_test_client(monkeypatch, store, gmail):
    app = FastAPI()
    app.include_router(
        messages_endpoint.router,
        prefix="/api/v1/conversations/{conversation_id}/messages",
    )
    app.dependency_overrides[get_current_user] = lambda: {
        "id": str(USER_ID),
    }
    app.dependency_overrides[get_db] = lambda: None
    app.dependency_overrides[messages_endpoint.get_message_service] = (
        lambda: store
    )
    monkeypatch.setattr(
        messages_endpoint,
        "GmailService",
        lambda _db: gmail,
    )
    return TestClient(app)


def test_valid_uuid_json_request_reaches_gmail_draft_save_service(monkeypatch):
    store = DraftSaveStore()
    gmail = DraftGmail()
    claim_calls = []
    original_claim = store.claim_gmail_draft_save

    def track_claim(**kwargs):
        claim_calls.append(kwargs)
        return original_claim(**kwargs)

    store.claim_gmail_draft_save = track_claim
    client = draft_save_test_client(monkeypatch, store, gmail)

    response = client.post(
        f"/api/v1/conversations/{CONVERSATION_ID}/messages/{MESSAGE_ID}/draft/save",
        json={
            "connection_id": VALID_CONNECTION_ID,
            "operation_id": VALID_OPERATION_ID,
        },
    )

    assert response.status_code == 200
    assert claim_calls[0]["connection_id"] == UUID(VALID_CONNECTION_ID)
    assert claim_calls[0]["operation_id"] == UUID(VALID_OPERATION_ID)
    assert gmail.create_calls[0]["connection_id"] == UUID(VALID_CONNECTION_ID)
    assert gmail.create_calls[0]["operation_id"] == UUID(VALID_OPERATION_ID)
    assert store.state == "saved"


@pytest.mark.parametrize(
    "field",
    ["connection_id", "operation_id"],
)
def test_invalid_uuid_json_request_never_reaches_gmail_or_marks_uncertain(
    monkeypatch,
    field,
):
    store = DraftSaveStore(state="not_started")
    gmail = DraftGmail()
    claim_calls = []
    original_claim = store.claim_gmail_draft_save

    def track_claim(**kwargs):
        claim_calls.append(kwargs)
        return original_claim(**kwargs)

    store.claim_gmail_draft_save = track_claim
    client = draft_save_test_client(monkeypatch, store, gmail)
    payload = {
        "connection_id": VALID_CONNECTION_ID,
        "operation_id": VALID_OPERATION_ID,
    }
    payload[field] = "not-a-uuid"

    response = client.post(
        f"/api/v1/conversations/{CONVERSATION_ID}/messages/{MESSAGE_ID}/draft/save",
        json=payload,
    )

    assert response.status_code == 422
    assert claim_calls == []
    assert gmail.create_calls == []
    assert store.finish_calls == []
    assert store.state == "not_started"


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


@pytest.mark.parametrize(
    "google_error",
    [
        GoogleAPIError(403, "permission denied", reason="insufficientPermissions"),
        GoogleAPIError(429, "quota exceeded", reason="rateLimitExceeded"),
        GoogleAPIError(503, "lookup timed out"),
    ],
)
def test_failed_status_lookup_stays_uncertain_and_never_creates(
    monkeypatch,
    google_error,
):
    store = DraftSaveStore(state="uncertain")
    gmail = DraftGmail()

    async def failed_lookup(**_kwargs):
        gmail.find_calls.append(_kwargs)
        raise google_error

    monkeypatch.setattr(gmail, "find_draft_by_operation_id", failed_lookup)
    monkeypatch.setattr(messages_endpoint, "GmailService", lambda _db: gmail)

    for _ in range(2):
        with pytest.raises(HTTPException) as check_error:
            asyncio.run(
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
        assert check_error.value.status_code == 503

    assert store.state == "uncertain"
    assert len(gmail.find_calls) == 2
    assert gmail.create_calls == []


def test_repeated_complete_no_match_checks_do_not_authorize_creation(monkeypatch):
    store = DraftSaveStore(state="uncertain")
    gmail = DraftGmail(found=None)
    monkeypatch.setattr(messages_endpoint, "GmailService", lambda _db: gmail)

    for _ in range(2):
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
        assert result.state == "uncertain"
        assert "does not prove creation failed" in result.detail

    assert len(gmail.find_calls) == 2
    assert gmail.create_calls == []


def test_incomplete_scan_remains_uncertain_and_does_not_create(monkeypatch):
    store = DraftSaveStore(state="uncertain")
    gmail = DraftGmail(complete_scan=False)
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

    assert result.state == "uncertain"
    assert "could not inspect the draft list completely" in result.detail
    assert gmail.create_calls == []


def test_operation_record_load_failure_keeps_status_uncertain(monkeypatch):
    store = DraftSaveStore(state="uncertain")
    gmail = DraftGmail()

    def fail_load(**_kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(store, "get_gmail_draft_save_record", fail_load)
    monkeypatch.setattr(messages_endpoint, "GmailService", lambda _db: gmail)

    with pytest.raises(HTTPException) as check_error:
        asyncio.run(
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

    assert check_error.value.status_code == 503
    assert "remains uncertain" in check_error.value.detail
    assert gmail.find_calls == []
    assert gmail.create_calls == []


def test_operation_claim_persistence_failure_prevents_gmail_request(monkeypatch):
    store = DraftSaveStore()
    gmail = DraftGmail()

    def fail_claim(**_kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(store, "claim_gmail_draft_save", fail_claim)
    monkeypatch.setattr(messages_endpoint, "GmailService", lambda _db: gmail)

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

    assert save_error.value.status_code == 503
    assert "Gmail was not called" in save_error.value.detail
    assert gmail.create_calls == []


def test_reconciliation_database_failure_keeps_operation_recoverable(monkeypatch):
    store = DraftSaveStore(state="uncertain")
    gmail = DraftGmail(found={
        "draft_id": "draft-confirmed",
        "message_id": "gmail-message-confirmed",
        "thread_id": "thread-confirmed",
    })

    def fail_persist(**_kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(store, "finish_gmail_draft_save", fail_persist)
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

    assert result.state == "saved"
    assert result.draft_id == "draft-confirmed"
    assert "could not be updated" in result.detail
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


def test_wrong_conversation_is_not_found_before_operation_lookup():
    service = object.__new__(MessageService)
    service.verify_conversation_ownership = lambda **_kwargs: False

    result = service.get_gmail_draft_save_record(
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        message_id=MESSAGE_ID,
        connection_id=CONNECTION_ID,
        operation_id=OPERATION_ID,
    )

    assert result == {"status": "not_found"}
