import logging
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient

from app.api import deps
from app.api.v1.endpoints import drive_browser as endpoint
from app.core.config import settings
from app.db.session import get_db
from app.integrations.google import scopes as google_scopes
from app.main import app
from app.schemas.drive_browser import DriveFileListResponse
from app.services.drive_browser_service import (
    DriveReadScopeMissingError,
)

USER_ID = uuid4()
OTHER_USER_ID = uuid4()
CONNECTION_ID = uuid4()
OTHER_CONNECTION_ID = uuid4()
FOLDER_ID = "1FolderAbCdEfGhIjKlMnOpQrStUvWxYz"
FAKE_TOKEN = "ya29.SECRET_ACCESS_TOKEN_VALUE"
SECRET_CONTENT = "AADHAAR-1234-5678-9012-CONFIDENTIAL"


def _auth(user_id=USER_ID):
    return {
        "id": str(user_id),
        "user_metadata": {"default_drive_connection_id": str(CONNECTION_ID)},
    }


@contextmanager
def _client(*, user=None, authenticated=True):
    previous = dict(app.dependency_overrides)
    if authenticated:
        app.dependency_overrides[deps.get_current_user] = lambda: user or _auth()
    app.dependency_overrides[get_db] = lambda: object()
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


def _empty_listing():
    return DriveFileListResponse(
        items=[],
        next_page_token=None,
        page_size=25,
        folder_id=None,
        query=None,
        order_by="folder, name",
    )


# ----------------------------------------------------------------------
# Feature gate: no route is a production bypass
# ----------------------------------------------------------------------


def test_every_drive_browser_endpoint_returns_the_existing_unavailable_response():
    expected = (
        "Google Drive integration is temporarily unavailable and will be "
        "available in a future update."
    )
    cases = [
        ("get", "/api/v1/drive-browser/status"),
        ("get", "/api/v1/drive-browser/files"),
        ("get", f"/api/v1/drive-browser/folders/{FOLDER_ID}"),
        ("get", f"/api/v1/drive-browser/folders/{FOLDER_ID}/children"),
    ]
    with _client() as client:
        for method, path in cases:
            res = getattr(client, method)(path)
            assert res.status_code == 503, (method, path, res.text)
            assert res.json().get("detail") == expected


def test_feature_gate_holds_even_when_runtime_flag_is_true(monkeypatch):
    monkeypatch.setattr(settings, "DRIVE_CONTRACTS_ENABLED", True)
    with _client() as client:
        res = client.get("/api/v1/drive-browser/files")
    assert res.status_code == 503


def test_no_drive_browser_route_has_a_user_id_or_connection_id_input():
    paths = {
        path: spec
        for path, spec in app.openapi()["paths"].items()
        if path.startswith("/api/v1/drive-browser")
    }
    assert paths, "Drive browser routes must be registered"
    for path, methods in paths.items():
        for method, spec in methods.items():
            params = {param["name"] for param in spec.get("parameters", [])}
            assert "user_id" not in params, (path, method)
            assert "connection_id" not in params, (path, method)

    all_paths = set(app.openapi()["paths"])
    assert "/api/v1/drive-browser/files/{file_id}/content" not in all_paths
    assert "/api/v1/drive-browser/files/{file_id}/text" not in all_paths


# ----------------------------------------------------------------------
# Identity comes only from authentication
# ----------------------------------------------------------------------


def test_unauthenticated_requests_are_rejected():
    with _client(authenticated=False) as client:
        for path in (
            "/api/v1/drive-browser/status",
            "/api/v1/drive-browser/files",
            f"/api/v1/drive-browser/folders/{FOLDER_ID}",
            f"/api/v1/drive-browser/folders/{FOLDER_ID}/children",
        ):
            assert client.get(path).status_code == 401


def test_forged_connection_and_user_params_cannot_change_service_identity(
    monkeypatch,
):
    calls = []

    class _FakeService:
        def __init__(self, db):
            pass

        async def list_files(self, **kwargs):
            calls.append(kwargs)
            return _empty_listing()

    monkeypatch.setattr(endpoint, "_raise_drive_unavailable", lambda: None)
    monkeypatch.setattr(endpoint, "DriveBrowserService", _FakeService)

    with _client() as client:
        res = client.get(
            "/api/v1/drive-browser/files",
            params={
                "user_id": str(OTHER_USER_ID),
                "connection_id": str(OTHER_CONNECTION_ID),
                "folder_id": FOLDER_ID,
            },
        )

    assert res.status_code == 200
    assert calls, "service must be invoked"
    assert calls[0]["user_id"] == USER_ID
    assert "connection_id" not in calls[0]
    assert calls[0]["folder_id"] == FOLDER_ID


def test_another_users_connection_hint_is_rejected_by_the_service():
    from app.services.drive_browser_service import (
        DriveBrowserService,
        DriveConnectionMissingError,
    )

    service = DriveBrowserService.__new__(DriveBrowserService)
    service.connection_repository = SimpleNamespace(
        get_active_google_connection=lambda **kwargs: None,
        list_by_user=lambda user_id: [],
    )
    service.credential_repository = SimpleNamespace(
        get_by_connection=lambda **kwargs: None
    )
    try:
        service.resolve_connection(USER_ID, OTHER_CONNECTION_ID)
    except DriveConnectionMissingError:
        pass
    else:
        raise AssertionError("forged connection id must be rejected")


# ----------------------------------------------------------------------
# Permission + content hygiene
# ----------------------------------------------------------------------


def test_missing_drive_readonly_scope_fails_closed(monkeypatch):
    monkeypatch.setattr(endpoint, "_raise_drive_unavailable", lambda: None)

    class _FakeService:
        def __init__(self, db):
            pass

        async def list_files(self, **kwargs):
            raise DriveReadScopeMissingError(
                "Google Drive read-only permission has not been granted."
            )

    monkeypatch.setattr(endpoint, "DriveBrowserService", _FakeService)
    with _client() as client:
        res = client.get("/api/v1/drive-browser/files")

    assert res.status_code == 403
    assert "read-only" in res.json()["detail"].lower()


def test_no_token_or_document_content_in_response_or_logs(caplog, monkeypatch):
    from app.services.drive_browser_service import (
        DriveBrowserService,
    )

    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(endpoint, "_raise_drive_unavailable", lambda: None)

    seen_params = {}

    class _FakeApiClient:
        async def request(self, **kwargs):
            seen_params.update(kwargs["params"])
            # A raw provider exception carrying secrets must never reach clients
            # or logs; it is translated by the service first.
            from app.integrations.google.exceptions import GoogleAPIError

            raise GoogleAPIError(status_code=403, message=FAKE_TOKEN)

    service = DriveBrowserService.__new__(DriveBrowserService)
    service.db = object()
    service.api_client = _FakeApiClient()
    service.connection_repository = SimpleNamespace(
        list_by_user=lambda user_id: [
            SimpleNamespace(
                id=CONNECTION_ID,
                provider="google",
                status="connected",
                email="owner@example.com",
            )
        ],
        get_active_google_connection=lambda **kwargs: None,
    )
    service.credential_repository = SimpleNamespace(
        get_by_connection=lambda **kwargs: SimpleNamespace(
            scopes=[google_scopes.DRIVE_READ_SCOPE]
        )
    )

    class _FakeService:
        def __init__(self, db):
            pass

        async def list_files(self, **kwargs):
            return await service.list_files(**kwargs)

    monkeypatch.setattr(endpoint, "DriveBrowserService", _FakeService)
    with _client() as client:
        res = client.get("/api/v1/drive-browser/files")

    assert res.status_code == 403
    assert FAKE_TOKEN not in res.text
    assert FAKE_TOKEN not in caplog.text
    assert SECRET_CONTENT not in caplog.text
    assert "capabilities(canDownload,canEdit)" in seen_params["fields"]
    assert "alt" not in seen_params
    assert "export" not in seen_params["q"]


def test_service_only_requests_metadata_fields_never_content():
    seen = {}

    class _FakeApiClient:
        async def request(self, **kwargs):
            seen.update(kwargs)
            return {"files": []}

    from app.services.drive_browser_service import DriveBrowserService

    service = DriveBrowserService.__new__(DriveBrowserService)
    service.db = object()
    service.api_client = _FakeApiClient()
    service.connection_repository = SimpleNamespace(
        list_by_user=lambda user_id: [
            SimpleNamespace(
                id=CONNECTION_ID, provider="google", status="connected"
            )
        ],
        get_active_google_connection=lambda **kwargs: None,
    )
    service.credential_repository = SimpleNamespace(
        get_by_connection=lambda **kwargs: SimpleNamespace(
            scopes=[google_scopes.DRIVE_READ_SCOPE]
        )
    )
    # Avoid creating a real DB session; the fake client ignores db.
    import app.services.drive_browser_service as module

    original_session = module.SessionLocal
    module.SessionLocal = lambda: SimpleNamespace(close=lambda: None)
    try:
        import asyncio

        asyncio.run(service.list_files(user_id=USER_ID))
    finally:
        module.SessionLocal = original_session

    assert seen["method"] == "GET"
    assert seen["url"] == "https://www.googleapis.com/drive/v3/files"
    assert seen["user_id"] == str(USER_ID)
    assert seen["connection_id"] == str(CONNECTION_ID)
    assert "alt" not in seen["params"]
    assert "supportsAllDrives" in seen["params"]
