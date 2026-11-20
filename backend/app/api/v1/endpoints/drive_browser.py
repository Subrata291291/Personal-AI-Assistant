"""Additive REST endpoints for the read-only Drive browser.

Every handler begins with an unconditional unavailable guard. These routes stay
disabled until a later activation phase lifts the existing Drive gates; no
alternate path here enables production Drive access.
"""

import logging
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, is_integration_enabled
from app.core.request_metrics import request_id
from app.db.session import get_db
from app.schemas.drive_browser import (
    DRIVE_FILE_ID_PATTERN,
    MAX_PAGE_TOKEN_LENGTH,
    DriveBrowserStatusResponse,
    DriveFile,
    DriveFileListResponse,
)
from app.services.drive_browser_service import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    DriveAPIUnavailableError,
    DriveAuthorizationExpiredError,
    DriveBrowserError,
    DriveBrowserService,
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
    SearchScope,
)

logger = logging.getLogger(__name__)

router = APIRouter()

DRIVE_FEATURE_UNAVAILABLE_DETAIL = (
    "Google Drive integration is temporarily unavailable and will be available "
    "in a future update."
)


def _raise_drive_unavailable() -> None:
    """Keep every Drive browser route behind the existing kill-switch."""
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


def _browser_error(exc: DriveBrowserError, operation: str) -> HTTPException:
    if isinstance(
        exc,
        (DriveConnectionMissingError, DriveConnectionSelectionRequiredError),
    ):
        http_exc = HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        )
    elif isinstance(exc, DriveReadScopeMissingError):
        http_exc = HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        )
    elif isinstance(exc, DriveInvalidFolderIdError):
        http_exc = HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )
    elif isinstance(
        exc,
        (
            DriveInvalidPageTokenError,
            DriveInvalidPageSizeError,
            DriveInvalidSearchError,
            DriveInvalidOrderByError,
        ),
    ):
        http_exc = HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )
    elif isinstance(exc, DriveFolderNotFoundError):
        http_exc = HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        )
    elif isinstance(exc, DrivePermissionDeniedError):
        http_exc = HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        )
    elif isinstance(exc, DriveAuthorizationExpiredError):
        http_exc = HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        )
    elif isinstance(exc, DriveRateLimitError):
        http_exc = HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(exc),
        )
    elif isinstance(exc, (DriveAPIUnavailableError, DriveInvalidResponseError)):
        http_exc = HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        )
    else:
        http_exc = HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="The Drive request could not be completed.",
        )

    # Log safe metadata only: never exception text, filenames, or tokens.
    logger.warning(
        "drive_browser_error | operation=%s | request_id=%s | error_type=%s "
        "| status=%s",
        operation,
        request_id(),
        type(exc).__name__,
        http_exc.status_code,
    )
    return http_exc


@router.get("/status", response_model=DriveBrowserStatusResponse)
def get_browser_status(
    current_user: CurrentUser,
    db: Session = Depends(get_db),
) -> DriveBrowserStatusResponse:
    _raise_drive_unavailable()
    return DriveBrowserService(db).get_status(
        user_id=_user_id(current_user),
        integration_enabled=is_integration_enabled(current_user, "drive"),
    )


@router.get("/files", response_model=DriveFileListResponse)
async def list_files(
    current_user: CurrentUser,
    db: Session = Depends(get_db),
    folder_id: Annotated[
        str | None,
        Query(pattern=DRIVE_FILE_ID_PATTERN, max_length=200),
    ] = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    search_scope: Annotated[
        SearchScope,
        Query(
            description=(
                "Use 'global' for Drive-wide name search or 'folder' to search "
                "only the displayed root/folder."
            )
        ),
    ] = "global",
    page_size: Annotated[
        int,
        Query(ge=1, le=MAX_PAGE_SIZE),
    ] = DEFAULT_PAGE_SIZE,
    page_token: Annotated[
        str | None,
        Query(max_length=MAX_PAGE_TOKEN_LENGTH),
    ] = None,
    order_by: Annotated[str | None, Query(max_length=120)] = None,
) -> DriveFileListResponse:
    _raise_drive_unavailable()
    try:
        return await DriveBrowserService(db).list_files(
            user_id=_user_id(current_user),
            folder_id=folder_id,
            query=q,
            search_scope=search_scope,
            page_size=page_size,
            page_token=page_token,
            order_by=order_by,
        )
    except DriveBrowserError as exc:
        raise _browser_error(exc, "list_files") from exc


@router.get("/folders/{folder_id}", response_model=DriveFile)
async def get_folder(
    folder_id: str,
    current_user: CurrentUser,
    db: Session = Depends(get_db),
) -> DriveFile:
    _raise_drive_unavailable()
    try:
        return await DriveBrowserService(db).get_folder(
            user_id=_user_id(current_user),
            folder_id=folder_id,
        )
    except DriveBrowserError as exc:
        raise _browser_error(exc, "get_folder") from exc


@router.get("/folders/{folder_id}/children", response_model=DriveFileListResponse)
async def list_folder_children(
    folder_id: str,
    current_user: CurrentUser,
    db: Session = Depends(get_db),
    q: Annotated[str | None, Query(max_length=200)] = None,
    page_size: Annotated[
        int,
        Query(ge=1, le=MAX_PAGE_SIZE),
    ] = DEFAULT_PAGE_SIZE,
    page_token: Annotated[
        str | None,
        Query(max_length=MAX_PAGE_TOKEN_LENGTH),
    ] = None,
    order_by: Annotated[str | None, Query(max_length=120)] = None,
) -> DriveFileListResponse:
    _raise_drive_unavailable()
    try:
        return await DriveBrowserService(db).list_folder_children(
            user_id=_user_id(current_user),
            folder_id=folder_id,
            query=q,
            page_size=page_size,
            page_token=page_token,
            order_by=order_by,
        )
    except DriveBrowserError as exc:
        raise _browser_error(exc, "list_folder_children") from exc
