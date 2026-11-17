from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, is_integration_enabled
from app.core.config import settings
from app.db.session import get_db
from app.integrations.google import scopes as google_scopes
from app.integrations.google.exceptions import GoogleAPIError
from app.repositories.oauth_credential_repository import (
    OAuthCredentialRepository,
)
from app.schemas.drive_contracts import (
    DrivePickerConfigResponse,
    DrivePickerTokenResponse,
    DriveSelectedFileCreate,
    DriveSelectedFileListResponse,
    DriveSelectedFileResponse,
)
from app.services.drive_contracts_service import (
    CONTRACT_MIME_TYPES,
    DriveConnectionMissingError,
    DriveConnectionSelectionRequiredError,
    DriveContractsError,
    DriveContractsService,
    DriveFileInaccessibleError,
    DriveFileNotSelectedError,
    DriveScopeMissingError,
    DriveUnsupportedFileError,
)
from app.services.oauth_credential_service import OAuthCredentialService
from app.services.user_connection_service import UserConnectionService

router = APIRouter()

DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
DRIVE_FEATURE_UNAVAILABLE_DETAIL = (
    "Google Drive integration is temporarily unavailable and will be available "
    "in a future update."
)


def _raise_drive_unavailable() -> None:
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=DRIVE_FEATURE_UNAVAILABLE_DETAIL,
    )


def _user_id(current_user: dict[str, Any]) -> UUID:
    try:
        return UUID(str(current_user["id"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unable to identify the authenticated user.",
        ) from exc


def _drive_connection_id(
    *,
    current_user: dict[str, Any],
    db: Session,
    requested_connection_id: UUID | None = None,
) -> UUID:
    user_id = _user_id(current_user)
    metadata = current_user.get("user_metadata")
    default_connection_id = (
        metadata.get("default_drive_connection_id")
        if isinstance(metadata, dict)
        else None
    )
    service = UserConnectionService(db)
    access = service.get_user_drive_access(
        user_id=user_id,
        default_connection_id=default_connection_id,
    )

    eligible_ids = {
        connection["id"]
        for connection in service.list_connections(user_id=user_id)
        if connection["provider"] == "google"
        and connection["status"] == "connected"
        and connection["capabilities"].get("drive_contracts_read")
    }

    if requested_connection_id is not None:
        if requested_connection_id not in eligible_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "The selected Google account does not have Drive "
                    "contracts access."
                ),
            )
        return requested_connection_id

    if access["selection_required"]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "More than one connected Google account has Drive access. "
                "Choose which account Drive should use."
            ),
        )

    if not access["connection_id"]:
        if access["read_scope_missing"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "Google Drive permission has not been granted for the "
                    "selected account."
                ),
            )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Google Drive is not connected. Connect a Google account in "
                "Connected Apps and grant Drive file access."
            ),
        )

    return UUID(str(access["connection_id"]))


def _require_picker_scope(
    *,
    db: Session,
    user_id: UUID,
    connection_id: UUID,
) -> None:
    """Keep the browser token narrow.

    Google Picker needs an OAuth token. Only return one for accounts that
    granted ``drive.file`` so a pre-existing broad Drive read grant is never
    exposed to the browser.
    """
    credential = OAuthCredentialRepository(db).get_by_connection(
        user_id=user_id,
        connection_id=connection_id,
    )
    granted_scopes = set(credential.scopes or []) if credential else set()
    if not granted_scopes & google_scopes.DRIVE_FILE_SCOPES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Google Picker requires Drive file access. Grant Drive file "
                "access for the selected account in Connected Apps."
            ),
        )


def _require_drive_enabled(current_user: dict[str, Any]) -> None:
    if not settings.DRIVE_CONTRACTS_ENABLED or not is_integration_enabled(
        current_user,
        "drive",
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Google Drive contracts are turned off in Connected Apps.",
        )


def _to_response(selected_file) -> DriveSelectedFileResponse:
    return DriveSelectedFileResponse(
        provider_file_id=selected_file.provider_file_id,
        name=selected_file.name,
        mime_type=selected_file.mime_type,
        modified_time=selected_file.modified_time,
        size_bytes=selected_file.size_bytes,
        selected_at=selected_file.selected_at,
    )


def _drive_error(exc: Exception) -> HTTPException:
    if isinstance(
        exc,
        (
            DriveConnectionMissingError,
            DriveConnectionSelectionRequiredError,
        ),
    ):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        )
    if isinstance(exc, DriveScopeMissingError):
        return HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        )
    if isinstance(
        exc,
        (DriveFileNotSelectedError, DriveFileInaccessibleError),
    ):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        )
    if isinstance(exc, DriveUnsupportedFileError):
        return HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=str(exc),
        )
    if isinstance(exc, GoogleAPIError):
        return HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Google Drive could not be reached. Please try again.",
        )
    if isinstance(exc, DriveContractsError):
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="The Drive request could not be completed.",
    )


@router.get(
    "/picker-config",
    response_model=DrivePickerConfigResponse,
)
def get_picker_config(current_user: CurrentUser) -> DrivePickerConfigResponse:
    _raise_drive_unavailable()
    reason = None
    enabled = settings.DRIVE_CONTRACTS_ENABLED and is_integration_enabled(
        current_user,
        "drive",
    )
    if not settings.GOOGLE_CLIENT_ID:
        enabled = False
        reason = "Google OAuth is not configured."
    elif not settings.GOOGLE_API_KEY:
        enabled = False
        reason = "GOOGLE_API_KEY is not configured."

    return DrivePickerConfigResponse(
        enabled=enabled,
        api_key=settings.GOOGLE_API_KEY if enabled else None,
        client_id=settings.GOOGLE_CLIENT_ID if enabled else None,
        scope=DRIVE_FILE_SCOPE,
        mime_types=list(CONTRACT_MIME_TYPES),
        reason=reason,
    )


@router.get(
    "/picker-token",
    response_model=DrivePickerTokenResponse,
)
async def get_picker_token(
    current_user: CurrentUser,
    response: Response,
    db: Session = Depends(get_db),
    connection_id: UUID | None = None,
) -> DrivePickerTokenResponse:
    _raise_drive_unavailable()
    """Return a short-lived Google access token for Google Picker.

    The token is returned only to the authenticated owner of the connection,
    is never logged, and must be used in memory only. It is not a refresh
    token and it is not persisted by the frontend.
    """
    _require_drive_enabled(current_user)
    user_id = _user_id(current_user)
    resolved_connection_id = _drive_connection_id(
        current_user=current_user,
        db=db,
        requested_connection_id=connection_id,
    )
    _require_picker_scope(
        db=db,
        user_id=user_id,
        connection_id=resolved_connection_id,
    )

    try:
        access_token = await OAuthCredentialService(
            db
        ).get_valid_access_token(
            user_id=user_id,
            connection_id=resolved_connection_id,
        )
        db.commit()
    except GoogleAPIError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Google Drive authorization is no longer valid. Reconnect "
                "the account in Connected Apps."
            ),
        ) from exc
    except Exception as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Google Drive authorization could not be prepared.",
        ) from exc

    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return DrivePickerTokenResponse(
        access_token=access_token,
        scope=DRIVE_FILE_SCOPE,
    )


@router.get(
    "/files",
    response_model=DriveSelectedFileListResponse,
)
def list_selected_files(
    current_user: CurrentUser,
    db: Session = Depends(get_db),
) -> DriveSelectedFileListResponse:
    _raise_drive_unavailable()
    _require_drive_enabled(current_user)
    user_id = _user_id(current_user)
    connection_id = _drive_connection_id(
        current_user=current_user,
        db=db,
    )
    files = DriveContractsService(db).list_selected_files(
        user_id=user_id,
        connection_id=connection_id,
    )
    return DriveSelectedFileListResponse(
        files=[_to_response(item) for item in files],
        connection_id=connection_id,
    )


@router.post(
    "/files",
    response_model=DriveSelectedFileResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register_selected_file(
    payload: DriveSelectedFileCreate,
    current_user: CurrentUser,
    db: Session = Depends(get_db),
) -> DriveSelectedFileResponse:
    _raise_drive_unavailable()
    _require_drive_enabled(current_user)
    user_id = _user_id(current_user)
    connection_id = _drive_connection_id(
        current_user=current_user,
        db=db,
        requested_connection_id=payload.connection_id,
    )

    try:
        selected_file = await DriveContractsService(
            db
        ).register_selected_file(
            user_id=user_id,
            connection_id=connection_id,
            file_id=payload.file_id,
        )
    except Exception as exc:
        db.rollback()
        raise _drive_error(exc) from exc

    return _to_response(selected_file)


@router.get(
    "/files/{file_id}",
    response_model=DriveSelectedFileResponse,
)
async def get_selected_file(
    file_id: str,
    current_user: CurrentUser,
    db: Session = Depends(get_db),
) -> DriveSelectedFileResponse:
    _raise_drive_unavailable()
    _require_drive_enabled(current_user)
    user_id = _user_id(current_user)

    try:
        stored, contract = await DriveContractsService(
            db
        ).get_selected_file_details(
            user_id=user_id,
            file_id=file_id,
        )
    except Exception as exc:
        raise _drive_error(exc) from exc

    return DriveSelectedFileResponse(
        provider_file_id=contract.provider_file_id,
        name=contract.name,
        mime_type=contract.mime_type,
        modified_time=contract.modified_time,
        size_bytes=contract.size_bytes,
        selected_at=stored.selected_at,
    )


@router.delete(
    "/files/{file_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def remove_selected_file(
    file_id: str,
    current_user: CurrentUser,
    db: Session = Depends(get_db),
) -> Response:
    _raise_drive_unavailable()
    """Remove a file from the app allowlist without touching Google Drive."""
    _require_drive_enabled(current_user)
    user_id = _user_id(current_user)

    try:
        removed = DriveContractsService(db).remove_selected_file(
            user_id=user_id,
            file_id=file_id,
        )
    except Exception as exc:
        db.rollback()
        raise _drive_error(exc) from exc

    if not removed:
        raise _drive_error(DriveFileNotSelectedError(
            "That Drive file has not been selected for this account."
        ))

    return Response(status_code=status.HTTP_204_NO_CONTENT)
