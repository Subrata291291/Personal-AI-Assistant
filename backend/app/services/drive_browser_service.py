"""Read-only Google Drive browser service.

The service lists files, searches by name, walks folders, and normalizes Drive
metadata. It never requests, stores, or returns document content, and it never
handles OAuth tokens directly: all provider traffic goes through the shared
``GoogleAPIClient.request()`` interface, which owns token retrieval and refresh.

Connection ownership and the exact ``drive.readonly`` grant are verified before
every provider call. All identity inputs originate from the authenticated user
via the API layer; a connection id may only be supplied as an internal hint and
is still validated against the authenticated user's own connections.
"""

import logging
import re
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import SessionLocal
from app.integrations.google import scopes as google_scopes
from app.integrations.google.api_client import GoogleAPIClient
from app.integrations.google.exceptions import (
    GoogleAPIError,
    GoogleOAuthAuthorizationError,
)
from app.repositories.oauth_credential_repository import (
    OAuthCredentialRepository,
)
from app.repositories.user_connection_repository import (
    UserConnectionRepository,
)
from app.schemas.drive_browser import (
    MAX_PAGE_TOKEN_LENGTH,
    DriveBrowserStatusResponse,
    DriveFile,
    DriveFileListResponse,
    DriveFolder,
)

logger = logging.getLogger(__name__)

DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"
DRIVE_FILES_URL = f"{DRIVE_API_BASE}/files"

FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
MY_DRIVE_ROOT_ID = "root"

# Search scope is explicit so Drive-wide name search can never be confused with
# a root or folder listing. "global" preserves the established Phase 1 search
# behaviour; "folder" scopes the search to folder_id (or My Drive root).
SearchScope = Literal["global", "folder"]
VALID_SEARCH_SCOPES: tuple[SearchScope, ...] = ("global", "folder")

DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100
MAX_SEARCH_LENGTH = 200

FILE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{3,200}$")
# Google Drive orderBy keys accepted by this browser. Keeping an allowlist
# prevents arbitrary provider-side sort expressions from being forwarded.
ALLOWED_ORDER_FIELDS = frozenset({
    "name",
    "name_natural",
    "modifiedTime",
    "createdTime",
    "folder",
    "quotaBytesUsed",
})
DEFAULT_ORDER_BY = "folder, name"

FILE_LIST_FIELDS = (
    "nextPageToken,files("
    "id,name,mimeType,modifiedTime,createdTime,size,parents,"
    "webViewLink,iconLink,trashed,capabilities(canDownload,canEdit))"
)
FILE_METADATA_FIELDS = (
    "id,name,mimeType,modifiedTime,createdTime,size,parents,"
    "webViewLink,iconLink,trashed,capabilities(canDownload,canEdit)"
)

DRIVE_UNAVAILABLE_REASON = (
    "Google Drive integration is temporarily unavailable and will be "
    "available in a future update."
)


class DriveBrowserError(ValueError):
    """Base class for safe, user-facing Drive browser errors."""


class DriveConnectionMissingError(DriveBrowserError):
    pass


class DriveConnectionSelectionRequiredError(DriveBrowserError):
    pass


class DriveReadScopeMissingError(DriveBrowserError):
    pass


class DriveInvalidFolderIdError(DriveBrowserError):
    pass


class DriveInvalidPageTokenError(DriveBrowserError):
    pass


class DriveInvalidPageSizeError(DriveBrowserError):
    pass


class DriveInvalidSearchError(DriveBrowserError):
    pass


class DriveInvalidOrderByError(DriveBrowserError):
    pass


class DriveFolderNotFoundError(DriveBrowserError):
    pass


class DrivePermissionDeniedError(DriveBrowserError):
    pass


class DriveAuthorizationExpiredError(DriveBrowserError):
    pass


class DriveRateLimitError(DriveBrowserError):
    pass


class DriveAPIUnavailableError(DriveBrowserError):
    pass


class DriveInvalidResponseError(DriveBrowserError):
    pass


def _escape_query_value(value: str) -> str:
    """Escape a user value for a Drive ``q`` string literal."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _non_negative_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


class DriveBrowserService:
    """User-scoped, read-only operations over the Drive v3 files resource."""

    def __init__(
        self,
        db: Session,
        api_client: GoogleAPIClient | None = None,
        connection_repository: UserConnectionRepository | None = None,
        credential_repository: OAuthCredentialRepository | None = None,
    ):
        self.db = db
        self.api_client = api_client or GoogleAPIClient()
        self.connection_repository = (
            connection_repository or UserConnectionRepository(db)
        )
        self.credential_repository = (
            credential_repository or OAuthCredentialRepository(db)
        )

    # ------------------------------------------------------------------
    # Validation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_file_id(folder_id: str) -> str:
        if (
            not isinstance(folder_id, str)
            or not FILE_ID_PATTERN.fullmatch(folder_id.strip())
        ):
            raise DriveInvalidFolderIdError(
                "The requested Drive folder ID is not valid."
            )
        return folder_id.strip()

    @staticmethod
    def _validate_page_token(page_token: str | None) -> str | None:
        if page_token is None:
            return None
        if (
            not isinstance(page_token, str)
            or not page_token
            or len(page_token) > MAX_PAGE_TOKEN_LENGTH
            or any(ch.isspace() or not ch.isprintable() for ch in page_token)
        ):
            raise DriveInvalidPageTokenError(
                "The Drive page token is not valid. Refresh the list and try again."
            )
        return page_token

    @staticmethod
    def _validate_page_size(page_size: int) -> int:
        if not isinstance(page_size, int) or isinstance(page_size, bool):
            raise DriveInvalidPageSizeError("The page size is not valid.")
        if page_size < 1 or page_size > MAX_PAGE_SIZE:
            raise DriveInvalidPageSizeError(
                f"The page size must be between 1 and {MAX_PAGE_SIZE}."
            )
        return page_size

    @staticmethod
    def _validate_search_query(query: str | None) -> str | None:
        if query is None:
            return None
        if not isinstance(query, str):
            raise DriveInvalidSearchError("The Drive search query is not valid.")
        term = query.strip()
        if not term:
            raise DriveInvalidSearchError(
                "The Drive search query cannot be empty."
            )
        if len(term) > MAX_SEARCH_LENGTH:
            raise DriveInvalidSearchError(
                f"The Drive search query cannot exceed {MAX_SEARCH_LENGTH} characters."
            )
        if any(ord(ch) < 32 for ch in term):
            raise DriveInvalidSearchError(
                "The Drive search query contains unsupported characters."
            )
        return term

    @staticmethod
    def _validate_search_scope(search_scope: SearchScope) -> SearchScope:
        if search_scope not in VALID_SEARCH_SCOPES:
            raise DriveInvalidSearchError("The Drive search scope is not valid.")
        return search_scope

    @staticmethod
    def _validate_order_by(order_by: str | None) -> str:
        if order_by is None:
            return DEFAULT_ORDER_BY
        if not isinstance(order_by, str) or not order_by.strip():
            raise DriveInvalidOrderByError("The Drive sort order is not valid.")
        clauses = []
        for raw_clause in order_by.split(","):
            parts = raw_clause.strip().split()
            if len(parts) > 2 or not parts:
                raise DriveInvalidOrderByError(
                    "The Drive sort order is not valid."
                )
            field = parts[0]
            if field not in ALLOWED_ORDER_FIELDS:
                raise DriveInvalidOrderByError(
                    "The Drive sort field is not supported."
                )
            direction = parts[1].lower() if len(parts) == 2 else "asc"
            if direction not in {"asc", "desc"}:
                raise DriveInvalidOrderByError(
                    "The Drive sort direction is not valid."
                )
            clauses.append(field if direction == "asc" else f"{field} desc")
        if not clauses:
            raise DriveInvalidOrderByError("The Drive sort order is not valid.")
        return ", ".join(clauses)

    @classmethod
    def _build_query(
        cls,
        query: str | None,
        folder_id: str | None,
        *,
        search_scope: SearchScope = "global",
    ) -> str:
        """Build a Drive ``q`` expression.

        Listings are always scoped to a single parent: My Drive root when
        ``folder_id`` is absent, otherwise the requested folder. This prevents a
        root listing from becoming a flattened view of nested folders.

        Only an explicit ``search_scope="global"`` name search (with no folder)
        omits the parent clause. That preserves the established Drive-wide
        search semantics and keeps it distinguishable from root listing.
        """
        resolved_scope = cls._validate_search_scope(search_scope)
        term = (
            cls._validate_search_query(query) if query is not None else None
        )
        scoped = (
            folder_id is not None
            or term is None
            or resolved_scope == "folder"
        )

        clauses: list[str] = []
        if scoped:
            parent_id = (
                MY_DRIVE_ROOT_ID
                if folder_id is None
                else cls._validate_file_id(folder_id)
            )
            clauses.append(f"'{_escape_query_value(parent_id)}' in parents")
        clauses.append("trashed = false")
        if term is not None:
            clauses.append(f"name contains '{_escape_query_value(term)}'")
        return " and ".join(clauses)

    # ------------------------------------------------------------------
    # Connection ownership + permission enforcement
    # ------------------------------------------------------------------

    def _has_read_scope(self, user_id: UUID, connection_id: UUID) -> bool:
        credential = self.credential_repository.get_by_connection(
            user_id=user_id,
            connection_id=connection_id,
        )
        granted_scopes = set(credential.scopes or []) if credential else set()
        # drive.file is explicitly NOT sufficient for browsing arbitrary files.
        return google_scopes.DRIVE_READ_SCOPE in granted_scopes

    def _owned_connections(self, user_id: UUID) -> list[Any]:
        connections = self.connection_repository.list_by_user(user_id)
        return [
            connection
            for connection in connections
            if getattr(connection, "provider", None) == "google"
            and getattr(connection, "status", None) == "connected"
        ]

    def resolve_connection(
        self,
        user_id: UUID,
        connection_id: UUID | None = None,
    ) -> UUID:
        """Resolve a connection owned by ``user_id`` with drive.readonly.

        ``connection_id`` is an internal hint only. When supplied it must still
        resolve through an ownership-checked repository lookup.
        """
        if connection_id is not None:
            connection = self.connection_repository.get_active_google_connection(
                user_id=user_id,
                connection_id=connection_id,
            )
            if connection is None:
                raise DriveConnectionMissingError(
                    "The connected Google account is not available for this user."
                )
            if not self._has_read_scope(user_id, connection_id):
                raise DriveReadScopeMissingError(
                    "Google Drive read-only permission has not been granted "
                    "for the selected account."
                )
            return connection_id

        connections = self._owned_connections(user_id)
        eligible = [
            connection.id
            for connection in connections
            if self._has_read_scope(user_id, connection.id)
        ]
        if len(eligible) == 1:
            return eligible[0]
        if not eligible:
            if connections:
                raise DriveReadScopeMissingError(
                    "Google Drive read-only permission has not been granted "
                    "for the connected account."
                )
            raise DriveConnectionMissingError(
                "Google Drive is not connected for this account."
            )
        raise DriveConnectionSelectionRequiredError(
            "More than one Google account has Drive read-only access. "
            "Select one in Connected Apps."
        )

    # ------------------------------------------------------------------
    # Provider calls
    # ------------------------------------------------------------------

    @staticmethod
    def _translate_provider_error(exc: GoogleAPIError) -> DriveBrowserError:
        if isinstance(exc, GoogleOAuthAuthorizationError) or (
            getattr(exc, "status_code", None) == 401
        ):
            return DriveAuthorizationExpiredError(
                "Google Drive authorization is no longer valid. Reconnect the "
                "account in Connected Apps."
            )
        status_code = getattr(exc, "status_code", None)
        if status_code == 403:
            return DrivePermissionDeniedError(
                "Access to this Drive item was denied."
            )
        if status_code == 404:
            return DriveFolderNotFoundError(
                "The requested Drive folder or file was not found."
            )
        if status_code == 429:
            return DriveRateLimitError(
                "Google Drive is rate limiting requests. Please try again shortly."
            )
        if isinstance(status_code, int) and status_code >= 500:
            return DriveAPIUnavailableError(
                "Google Drive could not be reached. Please try again."
            )
        return DriveAPIUnavailableError(
            "Google Drive could not complete the request."
        )

    async def _request_files(
        self,
        user_id: UUID,
        connection_id: UUID,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        api_db = SessionLocal()
        try:
            result = await self.api_client.request(
                db=api_db,
                user_id=str(user_id),
                connection_id=str(connection_id),
                method="GET",
                url=DRIVE_FILES_URL,
                params=params,
            )
        except GoogleAPIError as exc:
            raise self._translate_provider_error(exc) from exc
        finally:
            api_db.close()

        if not isinstance(result, dict):
            raise DriveInvalidResponseError(
                "Google Drive returned an unexpected response."
            )
        return result

    async def _list(
        self,
        *,
        user_id: UUID,
        folder_id: str | None,
        query: str | None,
        search_scope: SearchScope,
        page_size: int,
        page_token: str | None,
        order_by: str | None,
        connection_id: UUID | None,
    ) -> DriveFileListResponse:
        resolved_page_size = self._validate_page_size(page_size)
        resolved_token = self._validate_page_token(page_token)
        resolved_order_by = self._validate_order_by(order_by)
        resolved_scope = self._validate_search_scope(search_scope)
        drive_query = self._build_query(
            query, folder_id, search_scope=resolved_scope
        )
        if folder_id is not None:
            folder_id = self._validate_file_id(folder_id)
        connection = self.resolve_connection(user_id, connection_id)

        params: dict[str, Any] = {
            "q": drive_query,
            "pageSize": resolved_page_size,
            "fields": FILE_LIST_FIELDS,
            "orderBy": resolved_order_by,
            "supportsAllDrives": "false",
            "includeItemsFromAllDrives": "false",
        }
        if resolved_token is not None:
            params["pageToken"] = resolved_token

        result = await self._request_files(user_id, connection, params)
        raw_files = result.get("files", [])
        if not isinstance(raw_files, list):
            raise DriveInvalidResponseError(
                "Google Drive returned an unexpected file list."
            )

        items: list[DriveFile] = []
        for raw_file in raw_files:
            normalized = self._normalize_file(raw_file)
            if normalized is not None:
                items.append(normalized)

        next_page_token = result.get("nextPageToken")
        if not isinstance(next_page_token, str) or not next_page_token:
            next_page_token = None

        return DriveFileListResponse(
            items=items,
            next_page_token=next_page_token,
            page_size=resolved_page_size,
            folder_id=folder_id,
            query=self._validate_search_query(query),
            order_by=resolved_order_by,
        )

    async def list_files(
        self,
        *,
        user_id: UUID,
        folder_id: str | None = None,
        query: str | None = None,
        search_scope: SearchScope = "global",
        page_size: int = DEFAULT_PAGE_SIZE,
        page_token: str | None = None,
        order_by: str | None = None,
        connection_id: UUID | None = None,
    ) -> DriveFileListResponse:
        """List root files (or a folder's direct children) for one user.

        ``search_scope="global"`` preserves the established Drive-wide name
        search; use ``"folder"`` to search only the displayed root or folder.
        """
        return await self._list(
            user_id=user_id,
            folder_id=folder_id,
            query=query,
            search_scope=search_scope,
            page_size=page_size,
            page_token=page_token,
            order_by=order_by,
            connection_id=connection_id,
        )

    async def list_folder_children(
        self,
        *,
        user_id: UUID,
        folder_id: str,
        query: str | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        page_token: str | None = None,
        order_by: str | None = None,
        connection_id: UUID | None = None,
    ) -> DriveFileListResponse:
        """List the direct children of one folder (never recursive)."""
        return await self._list(
            user_id=user_id,
            folder_id=folder_id,
            query=query,
            search_scope="folder",
            page_size=page_size,
            page_token=page_token,
            order_by=order_by,
            connection_id=connection_id,
        )

    @staticmethod
    def _normalize_file(raw_file: Any) -> DriveFile | None:
        """Normalize one provider metadata item, skipping malformed entries."""
        if not isinstance(raw_file, dict):
            return None
        file_id = raw_file.get("id")
        name = raw_file.get("name")
        mime_type = raw_file.get("mimeType")
        if (
            not isinstance(file_id, str)
            or not FILE_ID_PATTERN.fullmatch(file_id)
            or not isinstance(name, str)
            or not name.strip()
            or not isinstance(mime_type, str)
            or not mime_type
        ):
            return None

        raw_parents = raw_file.get("parents")
        parents = (
            [item for item in raw_parents if isinstance(item, str)][:100]
            if isinstance(raw_parents, list)
            else []
        )
        capabilities = raw_file.get("capabilities")
        if not isinstance(capabilities, dict):
            capabilities = {}

        web_view_link = raw_file.get("webViewLink")
        icon_link = raw_file.get("iconLink")
        is_folder = mime_type == FOLDER_MIME_TYPE
        model = DriveFolder if is_folder else DriveFile
        return model(
            id=file_id,
            name=name.strip()[:1024],
            mime_type=mime_type,
            modified_time=_parse_datetime(raw_file.get("modifiedTime")),
            created_time=_parse_datetime(raw_file.get("createdTime")),
            size=_non_negative_int(raw_file.get("size")),
            parents=parents,
            web_view_link=(
                web_view_link if isinstance(web_view_link, str) else None
            ),
            icon_link=(icon_link if isinstance(icon_link, str) else None),
            trashed=raw_file.get("trashed") is True,
            can_download=capabilities.get("canDownload") is True,
            can_edit=capabilities.get("canEdit") is True,
        )

    async def get_folder(
        self,
        *,
        user_id: UUID,
        folder_id: str,
        connection_id: UUID | None = None,
    ) -> DriveFolder:
        """Return metadata (including parents) for one folder."""
        normalized_folder_id = self._validate_file_id(folder_id)
        connection = self.resolve_connection(user_id, connection_id)

        api_db = SessionLocal()
        try:
            result = await self.api_client.request(
                db=api_db,
                user_id=str(user_id),
                connection_id=str(connection),
                method="GET",
                url=f"{DRIVE_API_BASE}/files/{normalized_folder_id}",
                params={
                    "fields": FILE_METADATA_FIELDS,
                    "supportsAllDrives": "false",
                },
            )
        except GoogleAPIError as exc:
            raise self._translate_provider_error(exc) from exc
        finally:
            api_db.close()

        if not isinstance(result, dict):
            raise DriveInvalidResponseError(
                "Google Drive returned an unexpected response."
            )
        if result.get("id") != normalized_folder_id:
            raise DriveFolderNotFoundError(
                "The requested Drive folder was not found."
            )
        if result.get("trashed") is True:
            raise DriveFolderNotFoundError(
                "The requested Drive folder is in the trash."
            )

        normalized = self._normalize_file(result)
        if normalized is None or not isinstance(normalized, DriveFolder):
            raise DriveInvalidResponseError(
                "Google Drive did not return folder metadata."
            )
        return normalized

    def get_status(
        self,
        *,
        user_id: UUID,
        integration_enabled: bool,
        connection_id: UUID | None = None,
    ) -> DriveBrowserStatusResponse:
        """Summarize Drive availability without exposing credentials."""
        connections = self._owned_connections(user_id)
        eligible = [
            connection
            for connection in connections
            if self._has_read_scope(user_id, connection.id)
        ]
        feature_enabled = bool(settings.DRIVE_CONTRACTS_ENABLED)
        read_scope_granted = bool(eligible)
        selection_required = len(eligible) > 1
        connected = bool(connections)
        available = (
            feature_enabled
            and integration_enabled
            and read_scope_granted
            and not selection_required
        )

        if not feature_enabled or not integration_enabled:
            reason = DRIVE_UNAVAILABLE_REASON
        elif selection_required:
            reason = "Select a Google account for Drive in Connected Apps."
        elif not connected:
            reason = "Google Drive is not connected for this account."
        elif not read_scope_granted:
            reason = "Google Drive read-only permission has not been granted."
        else:
            reason = None

        resolved_connection = eligible[0] if len(eligible) == 1 else None
        email = None
        if resolved_connection is not None:
            email = getattr(resolved_connection, "email", None)

        return DriveBrowserStatusResponse(
            available=available,
            reason=reason,
            feature_enabled=feature_enabled,
            integration_enabled=integration_enabled,
            connected=connected,
            read_scope_granted=read_scope_granted,
            selection_required=selection_required,
            connection_id=(
                UUID(str(resolved_connection.id))
                if resolved_connection is not None
                and getattr(resolved_connection, "id", None) is not None
                else None
            ),
            connection_email=email if isinstance(email, str) else None,
        )
