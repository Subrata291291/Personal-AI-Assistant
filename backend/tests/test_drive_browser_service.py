import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.integrations.google import scopes as google_scopes
from app.integrations.google.exceptions import GoogleAPIError
from app.services import drive_browser_service as browser_module
from app.services.drive_browser_service import (
    DEFAULT_ORDER_BY,
    DEFAULT_PAGE_SIZE,
    DRIVE_FILES_URL,
    FILE_LIST_FIELDS,
    DriveAPIUnavailableError,
    DriveAuthorizationExpiredError,
    DriveConnectionMissingError,
    DriveConnectionSelectionRequiredError,
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
    DriveBrowserService,
)

USER_ID = uuid4()
CONNECTION_ID = uuid4()
OTHER_CONNECTION_ID = uuid4()
FOLDER_ID = "1FolderAbCdEfGhIjKlMnOpQrStUvWxYz"

GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
FOLDER_MIME = "application/vnd.google-apps.folder"


@pytest.fixture(autouse=True)
def _fake_session_factory(monkeypatch):
    session = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(browser_module, "SessionLocal", lambda: session)
    return session


def _connection(connection_id=CONNECTION_ID):
    return SimpleNamespace(
        id=connection_id,
        provider="google",
        status="connected",
        email="owner@example.com",
    )


def _raw_file(
    file_id="1FileAbCdEfGhIjKlMnOpQrStUvWxYz",
    name="Report.pdf",
    mime_type="application/pdf",
    **overrides,
):
    raw = {
        "id": file_id,
        "name": name,
        "mimeType": mime_type,
        "modifiedTime": "2026-01-02T03:04:05.000Z",
        "createdTime": "2025-12-01T00:00:00.000Z",
        "size": "2048",
        "parents": [FOLDER_ID],
        "webViewLink": "https://drive.google.com/file/d/view",
        "iconLink": "https://drive-thirdparty.googleusercontent.com/icon",
        "trashed": False,
        "capabilities": {"canDownload": True, "canEdit": False},
    }
    raw.update(overrides)
    return raw


def _service(
    *,
    scopes=(google_scopes.DRIVE_READ_SCOPE,),
    connections=None,
    request_result=None,
    request_error=None,
    get_folder_result=None,
):
    """Build a service with injected mocks, following existing Drive tests."""
    if connections is None:
        connections = [_connection()]

    api_client = SimpleNamespace(request=AsyncMock())
    if request_error is not None:
        api_client.request.side_effect = request_error
    else:
        api_client.request.return_value = (
        request_result if request_result is not None else {"files": [], "nextPageToken": None}
        )

    if get_folder_result is not None:
        api_client.request.side_effect = [get_folder_result]

    service = DriveBrowserService.__new__(DriveBrowserService)
    service.db = SimpleNamespace(commit=lambda: None, rollback=lambda: None)
    service.api_client = api_client
    service.connection_repository = SimpleNamespace(
        list_by_user=lambda user_id: list(connections),
        get_active_google_connection=lambda user_id, connection_id: next(
            (
                connection
                for connection in connections
                if connection.id == connection_id
                and getattr(connection, "provider", None) == "google"
                and getattr(connection, "status", None) == "connected"
            ),
            None,
        ),
    )
    service.credential_repository = SimpleNamespace(
        get_by_connection=lambda user_id, connection_id: (
            SimpleNamespace(scopes=list(scopes))
            if any(connection.id == connection_id for connection in connections)
            else None
        ),
    )
    return service


def _call(coro):
    return asyncio.run(coro)


# ----------------------------------------------------------------------
# List / query construction
# ----------------------------------------------------------------------


def test_list_root_files_uses_drive_files_endpoint_and_defaults():
    service = _service(request_result={"files": [_raw_file(trashed=False)], "nextPageToken": ""})
    response = _call(service.list_files(user_id=USER_ID))

    params = service.api_client.request.call_args.kwargs["params"]
    assert service.api_client.request.call_args.kwargs["url"] == DRIVE_FILES_URL
    assert params["q"] == "'root' in parents and trashed = false"
    # The returned item belongs to a nested folder in this fake payload, but the
    # provider query must remain pinned to My Drive root.
    assert FOLDER_ID not in params["q"]
    assert params["pageSize"] == DEFAULT_PAGE_SIZE
    assert params["orderBy"] == DEFAULT_ORDER_BY
    assert params["fields"] == FILE_LIST_FIELDS
    assert "pageToken" not in params
    assert response.items[0].name == "Report.pdf"
    assert response.items[0].size == 2048
    assert response.next_page_token is None
    assert response.page_size == DEFAULT_PAGE_SIZE


def test_list_direct_folder_children_uses_parent_query():
    service = _service()
    _call(service.list_folder_children(user_id=USER_ID, folder_id=FOLDER_ID))

    params = service.api_client.request.call_args.kwargs["params"]
    assert params["q"] == f"'{FOLDER_ID}' in parents and trashed = false"


def test_default_query_scopes_root_and_excludes_trashed_files():
    assert (
        DriveBrowserService._build_query(None, None)
        == "'root' in parents and trashed = false"
    )


def test_search_by_file_name_escapes_user_input():
    service = _service()
    _call(service.list_files(user_id=USER_ID, query="  O'Brien's \"Q\"  "))

    params = service.api_client.request.call_args.kwargs["params"]
    assert params["q"] == (
        "trashed = false and name contains 'O\\'Brien\\'s \"Q\"'"
    )
    assert "in parents" not in params["q"]


def test_explicit_global_search_keeps_drive_wide_semantics():
    service = _service()
    _call(
        service.list_files(
            user_id=USER_ID,
            query="resume",
            search_scope="global",
        )
    )

    params = service.api_client.request.call_args.kwargs["params"]
    assert params["q"] == "trashed = false and name contains 'resume'"
    assert "in parents" not in params["q"]


def test_root_scoped_search_stays_within_my_drive_root():
    service = _service()
    _call(
        service.list_files(
            user_id=USER_ID,
            query="resume",
            search_scope="folder",
        )
    )

    params = service.api_client.request.call_args.kwargs["params"]
    assert params["q"] == (
        "'root' in parents and trashed = false and name contains 'resume'"
    )


def test_folder_scoped_search_combines_both_clauses():
    service = _service()
    _call(
        service.list_files(
            user_id=USER_ID,
            folder_id=FOLDER_ID,
            query="resume",
        )
    )

    params = service.api_client.request.call_args.kwargs["params"]
    assert params["q"] == (
        f"'{FOLDER_ID}' in parents and trashed = false "
        "and name contains 'resume'"
    )


def test_folder_children_search_is_never_drive_wide():
    service = _service()
    _call(
        service.list_folder_children(
            user_id=USER_ID,
            folder_id=FOLDER_ID,
            query="resume",
        )
    )

    params = service.api_client.request.call_args.kwargs["params"]
    assert params["q"] == (
        f"'{FOLDER_ID}' in parents and trashed = false "
        "and name contains 'resume'"
    )


def test_invalid_search_scope_is_rejected():
    with pytest.raises(DriveInvalidSearchError):
        DriveBrowserService._build_query(
            "resume", None, search_scope="shared-with-me"
        )


def test_empty_search_does_not_silently_become_unrestricted_listing():
    service = _service()
    with pytest.raises(DriveInvalidSearchError):
        _call(service.list_files(user_id=USER_ID, query="   "))


def test_pagination_returns_next_token_and_forwards_page_token():
    service = _service(
        request_result={"files": [], "nextPageToken": "next-token-123"}
    )
    response = _call(
        service.list_files(user_id=USER_ID, page_token="opaque~token!42")
    )

    params = service.api_client.request.call_args.kwargs["params"]
    assert params["q"] == "'root' in parents and trashed = false"
    assert params["pageToken"] == "opaque~token!42"
    assert response.next_page_token == "next-token-123"


@pytest.mark.parametrize("page_size", [0, 101, -5, True, "25"])
def test_page_size_is_enforced(page_size):
    service = _service()
    with pytest.raises(DriveInvalidPageSizeError):
        _call(service.list_files(user_id=USER_ID, page_size=page_size))


def test_order_by_is_validated_and_forwarded():
    service = _service()
    _call(service.list_files(user_id=USER_ID, order_by="modifiedTime desc, name"))

    params = service.api_client.request.call_args.kwargs["params"]
    assert params["orderBy"] == "modifiedTime desc, name"


def test_order_by_rejects_unsupported_field_and_direction():
    service = _service()
    with pytest.raises(DriveInvalidOrderByError):
        _call(service.list_files(user_id=USER_ID, order_by="secretField desc"))
    with pytest.raises(DriveInvalidOrderByError):
        _call(service.list_files(user_id=USER_ID, order_by="name sideways"))


@pytest.mark.parametrize("token", ["", "has space", "line\nbreak", "x" * 4097])
def test_page_token_validation(token):
    service = _service()
    with pytest.raises(DriveInvalidPageTokenError):
        _call(service.list_files(user_id=USER_ID, page_token=token))


def test_invalid_folder_id_is_rejected():
    service = _service()
    with pytest.raises(DriveInvalidFolderIdError):
        _call(service.list_folder_children(user_id=USER_ID, folder_id="bad id!"))


# ----------------------------------------------------------------------
# Normalization
# ----------------------------------------------------------------------


def test_missing_optional_metadata_is_normalized_safely():
    raw = {
        "id": "1GoogleDocWithoutSize",
        "name": "Notes",
        "mimeType": GOOGLE_DOC_MIME,
        "trashed": False,
    }
    service = _service(request_result={"files": [raw]})
    response = _call(service.list_files(user_id=USER_ID))

    item = response.items[0]
    assert item.size is None
    assert item.parents == []
    assert item.web_view_link is None
    assert item.icon_link is None
    assert item.modified_time is None
    assert item.can_download is False
    assert item.is_folder is False


def test_folder_items_use_folder_schema_with_parents():
    raw = _raw_file(
        file_id=FOLDER_ID,
        name="Documents",
        mime_type=FOLDER_MIME,
        size=None,
    )
    service = _service(request_result={"files": [raw]})
    response = _call(service.list_files(user_id=USER_ID))

    item = response.items[0]
    assert item.is_folder is True
    assert item.parents == [FOLDER_ID]
    dumped = response.model_dump(mode="json")
    assert dumped["items"][0]["is_folder"] is True


def test_malformed_provider_entries_are_skipped():
    service = _service(
        request_result={"files": [None, {"id": "bad id!"}, _raw_file()]}
    )
    response = _call(service.list_files(user_id=USER_ID))
    assert len(response.items) == 1


def test_invalid_provider_response_raises_safe_error():
    service = _service(request_result=["not", "a", "dict"])
    with pytest.raises(DriveInvalidResponseError):
        _call(service.list_files(user_id=USER_ID))


def test_invalid_files_list_raises_safe_error():
    service = _service(request_result={"files": "not-a-list"})
    with pytest.raises(DriveInvalidResponseError):
        _call(service.list_files(user_id=USER_ID))


# ----------------------------------------------------------------------
# Folder metadata + navigation
# ----------------------------------------------------------------------


def test_get_folder_returns_metadata_and_parents_for_breadcrumbs():
    raw = _raw_file(
        file_id=FOLDER_ID,
        name="Documents",
        mime_type=FOLDER_MIME,
        size=None,
        parents=["root-parent-id"],
    )
    service = _service(get_folder_result=raw)
    folder = _call(service.get_folder(user_id=USER_ID, folder_id=FOLDER_ID))

    assert folder.id == FOLDER_ID
    assert folder.parents == ["root-parent-id"]
    assert folder.is_folder is True
    assert service.api_client.request.call_args.kwargs["url"].endswith(
        f"/files/{FOLDER_ID}"
    )


def test_get_folder_rejects_non_folder_metadata():
    service = _service(
        get_folder_result=_raw_file(file_id=FOLDER_ID, mime_type="application/pdf")
    )
    with pytest.raises(DriveInvalidResponseError):
        _call(service.get_folder(user_id=USER_ID, folder_id=FOLDER_ID))


def test_get_folder_handles_missing_parent_gracefully():
    raw = _raw_file(file_id=FOLDER_ID, mime_type=FOLDER_MIME, parents=None)
    service = _service(get_folder_result=raw)
    folder = _call(service.get_folder(user_id=USER_ID, folder_id=FOLDER_ID))
    assert folder.parents == []


# ----------------------------------------------------------------------
# Provider error mapping
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "status_code, expected",
    [
        (401, DriveAuthorizationExpiredError),
        (403, DrivePermissionDeniedError),
        (404, DriveFolderNotFoundError),
        (429, DriveRateLimitError),
        (500, DriveAPIUnavailableError),
        (503, DriveAPIUnavailableError),
    ],
)
def test_provider_errors_map_to_safe_errors(status_code, expected):
    error = GoogleAPIError(status_code=status_code, message="SECRET RAW DETAIL")
    service = _service(request_error=error)
    with pytest.raises(expected) as exc_info:
        _call(service.list_files(user_id=USER_ID))
    assert "SECRET RAW DETAIL" not in str(exc_info.value)


# ----------------------------------------------------------------------
# Permission + connection enforcement
# ----------------------------------------------------------------------


def test_drive_file_scope_is_not_treated_as_drive_readonly():
    service = _service(scopes=(google_scopes.DRIVE_FILE_SCOPE,))
    with pytest.raises(DriveReadScopeMissingError):
        _call(service.list_files(user_id=USER_ID))


def test_missing_credentials_fail_closed():
    service = _service()
    service.credential_repository.get_by_connection = lambda **kwargs: None
    with pytest.raises(DriveReadScopeMissingError):
        _call(service.list_files(user_id=USER_ID))


def test_another_users_connection_cannot_be_used():
    service = _service()
    with pytest.raises(DriveConnectionMissingError):
        _call(
            service.list_files(
                user_id=USER_ID,
                connection_id=OTHER_CONNECTION_ID,
            )
        )


def test_owned_connection_hint_is_still_scope_checked():
    service = _service(scopes=(google_scopes.DRIVE_FILE_SCOPE,))
    with pytest.raises(DriveReadScopeMissingError):
        _call(
            service.list_files(
                user_id=USER_ID,
                connection_id=CONNECTION_ID,
            )
        )


def test_multiple_eligible_connections_require_selection():
    service = _service(
        connections=[_connection(CONNECTION_ID), _connection(OTHER_CONNECTION_ID)]
    )
    with pytest.raises(DriveConnectionSelectionRequiredError):
        _call(service.list_files(user_id=USER_ID))


def test_no_owned_connection_fails_closed():
    service = _service(connections=[])
    with pytest.raises(DriveConnectionMissingError):
        _call(service.list_files(user_id=USER_ID))


def test_status_does_not_expose_tokens_or_credentials(monkeypatch):
    from app.core.config import settings

    # Pin the code-level default explicitly; a developer .env may override it.
    monkeypatch.setattr(settings, "DRIVE_CONTRACTS_ENABLED", False)
    service = _service()
    status = service.get_status(user_id=USER_ID, integration_enabled=True)

    payload = status.model_dump(mode="json")
    assert status.available is False  # gates remain active in Phase 1
    assert status.read_scope_granted is True
    assert status.connection_id == CONNECTION_ID
    assert "token" not in str(payload).lower()
    assert "secret" not in str(payload).lower()
    assert set(payload) == {
        "available",
        "reason",
        "feature_enabled",
        "integration_enabled",
        "connected",
        "read_scope_granted",
        "selection_required",
        "connection_id",
        "connection_email",
    }
