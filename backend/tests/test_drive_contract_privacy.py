import logging
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from app.api import deps
from app.api.v1.endpoints import drive
from app.db.session import get_db
from app.main import app
from app.services.drive_contracts_service import ExtractedContract, SelectedContract
from app.services.drive_text_extraction import ExtractedDocument
from app.workflows.drive_contracts_read import generate_drive_contracts_response


USER_ID = uuid4()
CONNECTION_ID = uuid4()
FILE_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz"
SECRET_TEXT = "TOP_SECRET_CONTRACT_CLAUSE_9B7X"


class _FakeSelectedFile:
    def __init__(self):
        self.provider_file_id = FILE_ID
        self.name = "Master Services Agreement.pdf"
        self.mime_type = "application/pdf"


class _FakeDriveContractsService:
    def __init__(self, _db):
        self.db = _db

    def list_selected_files(self, *, user_id, connection_id):
        assert user_id == USER_ID
        assert connection_id == CONNECTION_ID
        return [_FakeSelectedFile()]

    async def extract_selected_file_text(self, *, user_id, file_id, max_chars=None):
        assert user_id == USER_ID
        assert file_id == FILE_ID
        return ExtractedContract(
            contract=SelectedContract(
                provider_file_id=FILE_ID,
                name="Master Services Agreement.pdf",
                mime_type="application/pdf",
                modified_time=None,
                size_bytes=128,
            ),
            document=ExtractedDocument(
                text=SECRET_TEXT,
                extraction="pdf",
                page_count=1,
                truncated=False,
            ),
        )


def _override_auth():
    return {
        "id": str(USER_ID),
        "user_metadata": {
            "drive_enabled": True,
            "default_drive_connection_id": str(CONNECTION_ID),
        },
    }


def _override_db():
    yield None


class _LLMResponse:
    def __init__(self, content):
        self.content = content
        self.provider = "test-provider"
        self.model = "test-model"


@contextmanager
def _client(overrides=None):
    previous = dict(app.dependency_overrides)
    app.dependency_overrides[deps.get_current_user] = _override_auth
    app.dependency_overrides[get_db] = _override_db
    if overrides:
        app.dependency_overrides.update(overrides)
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


def test_full_contract_text_endpoint_is_not_exposed():
    with _client() as client:
        response = client.get(f"/api/v1/drive/files/{FILE_ID}/text")

    assert response.status_code in {404, 405}
    assert SECRET_TEXT not in response.text


def test_drive_listing_and_metadata_routes_still_registered():
    paths = set(app.openapi().get("paths", {}).keys())
    assert "/api/v1/drive/files" in paths
    assert "/api/v1/drive/files/{file_id}" in paths
    assert "/api/v1/drive/files/{file_id}/text" not in paths


def test_contract_qa_suppresses_tracing_and_does_not_log_contract_text(caplog):
    calls = {"tracing_entered": 0, "generate": 0}
    secret_prompt_fragment = None

    @contextmanager
    def _fake_suppress():
        calls["tracing_entered"] += 1
        yield

    class _FakeRouter:
        def generate(self, request):
            nonlocal secret_prompt_fragment
            calls["generate"] += 1
            joined = "\n".join(m.content for m in request.messages)
            secret_prompt_fragment = SECRET_TEXT if SECRET_TEXT in joined else None
            return _LLMResponse("The agreement states the confidential clause.")

    caplog.set_level(logging.DEBUG)
    with patch(
        "app.workflows.drive_contracts_read._suppress_contract_tracing",
        _fake_suppress,
    ), patch(
        "app.workflows.drive_contracts_read.llm_router",
        _FakeRouter(),
    ), patch(
        "app.workflows.drive_contracts_read.DriveContractsService",
        _FakeDriveContractsService,
    ):
        result = generate_drive_contracts_response(
            user_id=str(USER_ID),
            connection_id=str(CONNECTION_ID),
            message="What does the contract say?",
        )

    assert calls["tracing_entered"] == 1
    assert calls["generate"] == 1
    assert secret_prompt_fragment == SECRET_TEXT
    assert "confidential clause" in result["assistant_response"].lower()
    assert SECRET_TEXT not in caplog.text


def test_tracing_suppression_context_managers_are_invoked(monkeypatch):
    events = []

    @contextmanager
    def _ls_ctx(*, enabled):
        events.append(("langsmith", enabled))
        yield

    @contextmanager
    def _lc_ctx(enabled):
        events.append(("langchain_core", enabled))
        yield

    class _Token:
        pass

    token = _Token()

    def _set(_value):
        events.append(("callback_set", _value))
        return token

    def _reset(_token):
        events.append(("callback_reset", _token))

    monkeypatch.setitem(
        __import__("sys").modules,
        "langsmith",
        SimpleNamespace(tracing_context=_ls_ctx),
    )
    monkeypatch.setitem(
        __import__("sys").modules,
        "langchain_core.tracers.context",
        SimpleNamespace(tracing_v2_enabled=_lc_ctx),
    )
    monkeypatch.setitem(
        __import__("sys").modules,
        "langchain_core.callbacks.manager",
        SimpleNamespace(
            tracing_v2_callback_var=SimpleNamespace(set=_set, reset=_reset)
        ),
    )

    # Import after monkeypatching module entries to exercise best-effort hooks.
    from app.workflows import drive_contracts_read as dcr

    with dcr._suppress_contract_tracing():
        events.append(("body", True))

    assert ("langsmith", False) in events
    assert ("langchain_core", False) in events
    assert ("callback_set", None) in events
    assert ("callback_reset", token) in events
    assert ("body", True) in events
