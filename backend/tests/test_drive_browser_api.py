from contextlib import contextmanager
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.api.v1.endpoints import drive_browser as endpoint
from app.db.session import get_db
from app.main import app
from app.schemas.drive_browser import DriveFileListResponse
from app.services.drive_browser_service import (
    DriveAPIUnavailableError,
    DriveAuthorizationExpiredError,
    DriveBrowserError,
    DriveConnectionMissingError,
    DriveFolderNotFoundError,
    DriveInvalidFolderIdError,
    DriveInvalidOrderByError,
    DriveInvalidPageSizeError,
    DriveInvalidPageTokenError,
    DriveInvalidResponseError,
    DriveInvalidSearchError,
    DrivePermissionDeniedError,
    DriveRateLimitError,
    DriveReadScopeMissingError,
)

USER_ID = uuid4()
FOLDER_ID = "1FolderAbCdEfGhIjKlMnOpQrStUvWxYz"
RAW_PROVIDER_SECRET = "quota_exceeded_internal_token_abc123"


@contextmanager
def _client():
    previous = dict(app.dependency_overrides)
    app.dependency_overrides[deps.get_current_user] = lambda: {
        "id": str(USER_ID),
        "user_metadata": {},
    }
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


def test_unauthenticated_request_is_rejected():
    client = TestClient(app)
    assert client.get("/api/v1/drive-browser/files").status_code == 401


def test_disabled_drive_returns_existing_safe_unavailable_response():
    with _client() as client:
        for path in (
            "/api/v1/drive-browser/status",
            "/api/v1/drive-browser/files",
            f"/api/v1/drive-browser/folders/{FOLDER_ID}",
            f"/api/v1/drive-browser/folders/{FOLDER_ID}/children",
        ):
            res = client.get(path)
            assert res.status_code == 503
            assert "temporarily unavailable" in res.json()["detail"].lower()


@pytest.mark.parametrize(
    "query_string",
    [
        "page_size=0",
        "page_size=101",
        "page_size=not-a-number",
        f"folder_id={'bad id!'}",
        "q=" + "x" * 201,
        "search_scope=shared-with-me",
    ],
)
def test_invalid_query_parameters_return_validation_errors(query_string):
    with _client() as client:
        res = client.get(f"/api/v1/drive-browser/files?{query_string}")
    assert res.status_code == 422


def test_page_token_length_is_validated_at_the_boundary():
    with _client() as client:
        res = client.get("/api/v1/drive-browser/files?page_token=" + "x" * 5000)
    assert res.status_code == 422


@pytest.mark.parametrize(
    "query_string, expected_scope",
    [
        ("q=resume", "global"),
        ("q=resume&search_scope=folder", "folder"),
    ],
)
def test_search_scope_is_explicit_at_the_api_boundary(
    monkeypatch, query_string, expected_scope
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
        res = client.get(f"/api/v1/drive-browser/files?{query_string}")

    assert res.status_code == 200
    assert calls[0]["search_scope"] == expected_scope


@pytest.mark.parametrize(
    "error, expected_status",
    [
        (DriveConnectionMissingError("missing"), 409),
        (DriveReadScopeMissingError("scope"), 403),
        (DriveInvalidFolderIdError("bad id"), 400),
        (DriveInvalidPageTokenError("bad token"), 400),
        (DriveInvalidPageSizeError("bad size"), 400),
        (DriveInvalidSearchError("bad search"), 400),
        (DriveInvalidOrderByError("bad sort"), 400),
        (DriveFolderNotFoundError("not found"), 404),
        (DrivePermissionDeniedError("denied"), 403),
        (DriveAuthorizationExpiredError("expired"), 409),
        (DriveRateLimitError("slow down"), 429),
        (DriveAPIUnavailableError("offline"), 502),
        (DriveInvalidResponseError("bad response"), 502),
        (DriveBrowserError("unknown"), 500),
    ],
)
def test_error_mapping_returns_predictable_status(error, expected_status):
    http_exc = endpoint._browser_error(error, "test_operation")
    assert http_exc.status_code == expected_status


def test_api_error_response_does_not_expose_raw_provider_exception(monkeypatch):
    from app.integrations.google.exceptions import GoogleAPIError
    from app.services.drive_browser_service import DriveBrowserService

    monkeypatch.setattr(endpoint, "_raise_drive_unavailable", lambda: None)

    class _FakeService:
        def __init__(self, db):
            pass

        async def list_files(self, **kwargs):
            raw = GoogleAPIError(status_code=429, message=RAW_PROVIDER_SECRET)
            raise DriveBrowserService._translate_provider_error(raw)

    monkeypatch.setattr(endpoint, "DriveBrowserService", _FakeService)
    with _client() as client:
        res = client.get("/api/v1/drive-browser/files")

    assert res.status_code == 429
    assert RAW_PROVIDER_SECRET not in res.text
    assert "rate limiting" in res.json()["detail"].lower()
