from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.api.v1.endpoints import chat as chat_endpoint
from app.main import app
from app.services.chat_service import ChatService


def _service(message_service, request_repository):
    return ChatService(
        message_service=message_service,
        user_settings_service=SimpleNamespace(get_settings=lambda **_k: SimpleNamespace(memory_enabled=False)),
        memory_pipeline=SimpleNamespace(process=lambda *_a, **_k: None),
        request_repository=request_repository,
    )


USER_ID = UUID("00000000-0000-0000-0000-000000000111")
OTHER_USER_ID = UUID("00000000-0000-0000-0000-000000000999")
CONVERSATION_ID = UUID("00000000-0000-0000-0000-000000000222")
OTHER_CONVERSATION_ID = UUID("00000000-0000-0000-0000-000000000333")


class FakeRepo:
    def __init__(self, record):
        self.record = record
        self.reconcile_calls = 0

    def get_request(self, *, user_id, conversation_id, idempotency_key):
        if self.record is None:
            return None
        if (
            str(self.record.get("user_id")) == str(user_id)
            and str(self.record.get("conversation_id")) == str(conversation_id)
            and str(self.record.get("idempotency_key")) == str(idempotency_key)
        ):
            return dict(self.record)
        return None

    def reconcile_completed_request(self, *, user_id, conversation_id, idempotency_key, user_message_id, assistant_message_id):
        self.reconcile_calls += 1
        rec = self.get_request(user_id=user_id, conversation_id=conversation_id, idempotency_key=idempotency_key)
        if rec is None:
            return None
        rec.update({
            "status": "completed",
            "user_message_id": user_message_id,
            "assistant_message_id": assistant_message_id,
            "error_message": None,
            "worker_id": None,
            "lease_expires_at": None,
        })
        self.record.update(rec)
        return rec


class FakeMessageService:
    def __init__(self, user_msg=None, assistant_msg=None, request_assistant=None):
        self.user_msg = user_msg
        self.assistant_msg = assistant_msg
        self.request_assistant = request_assistant
        self.get_request_message_calls = 0

    def get_message(self, *, user_id, conversation_id, message_id):
        if self.user_msg and str(message_id) == str(self.user_msg["id"]):
            return self.user_msg
        if self.assistant_msg and str(message_id) == str(self.assistant_msg["id"]):
            return self.assistant_msg
        raise LookupError("not found")

    def get_request_message(self, *, user_id, conversation_id, chat_request_id, role):
        self.get_request_message_calls += 1
        if role == "assistant":
            return self.request_assistant
        return None

    def get_conversation_messages(self, **_kwargs):
        msgs = [m for m in [self.user_msg, self.assistant_msg] if m]
        return msgs


def _completed_messages(conv_id):
    uid = uuid4()
    aid = uuid4()
    user_msg = {
        "id": uid,
        "conversation_id": str(conv_id),
        "role": "user",
        "content": "show me subbhro number",
        "metadata": {},
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    assistant_msg = {
        "id": aid,
        "conversation_id": str(conv_id),
        "role": "assistant",
        "content": "Subbhro Das: 111-111",
        "metadata": {"provider": "gemini", "model": "gemini-model"},
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    return user_msg, assistant_msg


@pytest.fixture
def client():
    app.dependency_overrides[deps.get_current_user] = lambda: {
        "id": str(USER_ID),
        "user_metadata": {},
    }
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_owner_can_retrieve_completed_chat_request_status(client):
    idem = uuid4()
    user_msg, assistant_msg = _completed_messages(CONVERSATION_ID)
    record = {
        "id": uuid4(),
        "user_id": USER_ID,
        "conversation_id": CONVERSATION_ID,
        "idempotency_key": idem,
        "status": "completed",
        "user_message_id": user_msg["id"],
        "assistant_message_id": assistant_msg["id"],
        "error_message": None,
    }
    svc = _service(FakeMessageService(user_msg, assistant_msg), FakeRepo(record))
    app.dependency_overrides[chat_endpoint.get_chat_service] = lambda: svc
    try:
        res = client.get(f"/api/v1/chat/requests/{idem}", params={"conversation_id": str(CONVERSATION_ID)})
        assert res.status_code == 200
        body = res.json()
        assert body["status"] == "completed"
        assert str(body["conversation_id"]) == str(CONVERSATION_ID)
        assert str(body["idempotency_key"]) == str(idem)
        assert body["response"]["assistant_message"]["content"] == "Subbhro Das: 111-111"
        assert body["response"]["provider"] == "gemini"
    finally:
        app.dependency_overrides.pop(chat_endpoint.get_chat_service, None)


def test_other_user_cannot_retrieve_chat_request_status(client):
    idem = uuid4()
    record = {
        "id": uuid4(),
        "user_id": OTHER_USER_ID,
        "conversation_id": CONVERSATION_ID,
        "idempotency_key": idem,
        "status": "processing",
        "user_message_id": None,
        "assistant_message_id": None,
        "error_message": None,
    }
    svc = _service(FakeMessageService(), FakeRepo(record))
    app.dependency_overrides[chat_endpoint.get_chat_service] = lambda: svc
    try:
        res = client.get(f"/api/v1/chat/requests/{idem}", params={"conversation_id": str(CONVERSATION_ID)})
        assert res.status_code == 404
        assert res.json().get("detail") == "Chat request not found."
    finally:
        app.dependency_overrides.pop(chat_endpoint.get_chat_service, None)


def test_wrong_conversation_id_cannot_retrieve_chat_request_status(client):
    idem = uuid4()
    record = {
        "id": uuid4(),
        "user_id": USER_ID,
        "conversation_id": CONVERSATION_ID,
        "idempotency_key": idem,
        "status": "processing",
        "user_message_id": None,
        "assistant_message_id": None,
        "error_message": None,
    }
    svc = _service(FakeMessageService(), FakeRepo(record))
    app.dependency_overrides[chat_endpoint.get_chat_service] = lambda: svc
    try:
        res = client.get(f"/api/v1/chat/requests/{idem}", params={"conversation_id": str(OTHER_CONVERSATION_ID)})
        assert res.status_code == 404
        assert res.json().get("detail") == "Chat request not found."
    finally:
        app.dependency_overrides.pop(chat_endpoint.get_chat_service, None)


def test_unknown_idempotency_key_returns_sanitized_not_found(client):
    svc = _service(FakeMessageService(), FakeRepo(None))
    app.dependency_overrides[chat_endpoint.get_chat_service] = lambda: svc
    try:
        res = client.get(f"/api/v1/chat/requests/{uuid4()}", params={"conversation_id": str(CONVERSATION_ID)})
        assert res.status_code == 404
        assert res.json().get("detail") == "Chat request not found."
    finally:
        app.dependency_overrides.pop(chat_endpoint.get_chat_service, None)


def test_processing_request_status_does_not_trigger_duplicate_execution(client):
    idem = uuid4()
    repo = FakeRepo({
        "id": uuid4(),
        "user_id": USER_ID,
        "conversation_id": CONVERSATION_ID,
        "idempotency_key": idem,
        "status": "processing",
        "user_message_id": None,
        "assistant_message_id": None,
        "error_message": None,
        "lease_expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
    })
    msg_svc = FakeMessageService()
    svc = _service(msg_svc, repo)
    app.dependency_overrides[chat_endpoint.get_chat_service] = lambda: svc
    try:
        res = client.get(f"/api/v1/chat/requests/{idem}", params={"conversation_id": str(CONVERSATION_ID)})
        assert res.status_code == 200
        body = res.json()
        assert body["status"] == "processing"
        assert body["response"] is None
        # Read-only status path should not reconcile/finalize or mutate execution state.
        assert repo.reconcile_calls == 0
    finally:
        app.dependency_overrides.pop(chat_endpoint.get_chat_service, None)


def test_expired_processing_request_reconciles_safely_without_rerun(client):
    idem = uuid4()
    user_msg, assistant_msg = _completed_messages(CONVERSATION_ID)
    request_id = uuid4()
    assistant_msg["chat_request_id"] = str(request_id)
    repo = FakeRepo({
        "id": request_id,
        "user_id": USER_ID,
        "conversation_id": CONVERSATION_ID,
        "idempotency_key": idem,
        "status": "processing",
        "user_message_id": user_msg["id"],
        "assistant_message_id": None,
        "error_message": None,
        "lease_expires_at": datetime.now(timezone.utc) - timedelta(seconds=5),
    })
    msg_svc = FakeMessageService(user_msg=user_msg, assistant_msg=assistant_msg, request_assistant=assistant_msg)
    svc = _service(msg_svc, repo)
    app.dependency_overrides[chat_endpoint.get_chat_service] = lambda: svc
    try:
        res = client.get(f"/api/v1/chat/requests/{idem}", params={"conversation_id": str(CONVERSATION_ID)})
        assert res.status_code == 200
        body = res.json()
        # Reconciled from persisted assistant message; no LLM/tool rerun, just finalization/read.
        assert body["status"] == "completed"
        assert body["response"]["assistant_message"]["content"] == "Subbhro Das: 111-111"
        assert repo.reconcile_calls == 1
    finally:
        app.dependency_overrides.pop(chat_endpoint.get_chat_service, None)


def test_failed_request_status_returns_terminal_error_without_duplicate_execution(client):
    idem = uuid4()
    repo = FakeRepo({
        "id": uuid4(),
        "user_id": USER_ID,
        "conversation_id": CONVERSATION_ID,
        "idempotency_key": idem,
        "status": "failed",
        "user_message_id": None,
        "assistant_message_id": None,
        "error_message": "previous failure",
    })
    svc = _service(FakeMessageService(), repo)
    app.dependency_overrides[chat_endpoint.get_chat_service] = lambda: svc
    try:
        res = client.get(f"/api/v1/chat/requests/{idem}", params={"conversation_id": str(CONVERSATION_ID)})
        assert res.status_code == 200
        body = res.json()
        assert body["status"] == "failed"
        assert body["error_message"] == "previous failure"
        assert body["response"] is None
        assert repo.reconcile_calls == 0
    finally:
        app.dependency_overrides.pop(chat_endpoint.get_chat_service, None)
