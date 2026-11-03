from urllib.parse import quote
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Response,
    status,
)
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser
from app.db.session import get_db
from app.integrations.google.exceptions import GoogleAPIError
from app.schemas.gmail import (
    GmailForwardRequest,
    GmailMessageActionRequest,
    GmailMessageDetailResponse,
    GmailMessageListResponse,
    GmailReplyRequest,
    GmailSendRequest,
    GmailTrashRequest,
)
from app.services.gmail_service import GmailService


router = APIRouter()


SUPPORTED_GMAIL_FOLDERS = {
    "inbox",
    "starred",
    "important",
    "sent",
    "drafts",
    "all",
    "spam",
    "trash",
}


def _get_user_id(current_user: dict) -> UUID:
    try:
        return UUID(str(current_user["id"]))
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unable to identify authenticated user.",
        ) from exc


def _handle_gmail_error(exc: Exception) -> None:
    if isinstance(exc, PermissionError):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc

    if isinstance(exc, ValueError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    if isinstance(exc, GoogleAPIError):
        if exc.status_code == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Gmail message not found.",
            ) from exc

        if exc.status_code == 403:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Google denied access to this Gmail resource.",
            ) from exc

        if exc.status_code == 429 or exc.retryable:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Gmail service is temporarily unavailable.",
            ) from exc

        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Gmail API request failed.",
        ) from exc

    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Unable to process Gmail request.",
    ) from exc


@router.get(
    "/messages",
    response_model=GmailMessageListResponse,
)
async def list_gmail_messages(
    current_user: CurrentUser,
    connection_id: UUID,
    max_results: int = Query(default=20, ge=1, le=50),
    page_token: str | None = None,
    query: str | None = Query(default=None, max_length=500),
    folder: str = Query(default="inbox", max_length=30),
    db: Session = Depends(get_db),
):
    """List messages in the authenticated user's connected Gmail account."""
    user_id = _get_user_id(current_user)
    folder = folder.strip().lower()

    if folder not in SUPPORTED_GMAIL_FOLDERS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Unsupported Gmail folder. "
                f"Supported folders: {', '.join(sorted(SUPPORTED_GMAIL_FOLDERS))}"
            ),
        )

    service = GmailService(db)

    try:
        return await service.list_messages(
            user_id=user_id,
            connection_id=connection_id,
            max_results=max_results,
            page_token=page_token,
            query=query,
            folder=folder,
        )
    except HTTPException:
        raise
    except Exception as exc:
        _handle_gmail_error(exc)


@router.get(
    "/messages/{message_id}",
    response_model=GmailMessageDetailResponse,
)
async def get_gmail_message(
    message_id: str,
    current_user: CurrentUser,
    connection_id: UUID,
    db: Session = Depends(get_db),
):
    """Retrieve the full details of one Gmail message."""
    user_id = _get_user_id(current_user)
    service = GmailService(db)

    try:
        message = await service.get_message(
            user_id=user_id,
            connection_id=connection_id,
            message_id=message_id,
        )
        return {"message": message}
    except HTTPException:
        raise
    except Exception as exc:
        _handle_gmail_error(exc)


@router.get(
    "/messages/{message_id}/attachments/{attachment_id}",
    response_model=None,
)
async def download_gmail_attachment(
    message_id: str,
    attachment_id: str,
    current_user: CurrentUser,
    connection_id: UUID,
    db: Session = Depends(get_db),
):
    """Download an attachment after verifying it belongs to the message."""
    user_id = _get_user_id(current_user)
    service = GmailService(db)

    try:
        message = await service.get_message(
            user_id=user_id,
            connection_id=connection_id,
            message_id=message_id,
        )

        attachments = message.get("attachments", [])
        attachment_metadata = next(
            (
                item
                for item in attachments
                if item.get("attachment_id") == attachment_id
            ),
            None,
        )

        if attachment_metadata is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Attachment not found in this Gmail message.",
            )

        file_bytes = await service.download_attachment(
            user_id=user_id,
            connection_id=connection_id,
            message_id=message_id,
            attachment_id=attachment_id,
        )

        filename = str(
            attachment_metadata.get("filename") or "attachment"
        )
        filename = filename.replace("\\", "/").split("/")[-1]
        filename = "".join(
            char
            for char in filename
            if char.isprintable() and char not in '\\r\\n"'
        ).strip()

        if not filename or filename in {".", ".."}:
            filename = "attachment"

        encoded_filename = quote(filename, safe="")

        return Response(
            content=file_bytes,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": (
                    'attachment; filename="attachment"; '
                    f"filename*=UTF-8''{encoded_filename}"
                ),
                "Content-Length": str(len(file_bytes)),
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )
    except HTTPException:
        raise
    except Exception as exc:
        _handle_gmail_error(exc)


@router.post(
    "/messages/send",
    status_code=status.HTTP_200_OK,
)
async def send_gmail_message(
    payload: GmailSendRequest,
    current_user: CurrentUser,
    db: Session = Depends(get_db),
):
    """Send a new email using the authenticated user's Gmail account."""
    user_id = _get_user_id(current_user)
    service = GmailService(db)

    try:
        return await service.send_message(
            user_id=user_id,
            connection_id=UUID(str(payload.connection_id)),
            to=[str(email) for email in payload.to],
            cc=[str(email) for email in payload.cc],
            bcc=[str(email) for email in payload.bcc],
            subject=payload.subject,
            body=payload.body,
            html_body=payload.html_body,
            reply_to=str(payload.reply_to) if payload.reply_to else None,
        )
    except HTTPException:
        raise
    except Exception as exc:
        _handle_gmail_error(exc)


@router.post(
    "/messages/{message_id}/trash",
    status_code=status.HTTP_200_OK,
)
async def trash_gmail_message(
    message_id: str,
    payload: GmailTrashRequest,
    current_user: CurrentUser,
    db: Session = Depends(get_db),
):
    """Move a Gmail message to Trash for the authenticated user."""
    user_id = _get_user_id(current_user)
    service = GmailService(db)

    try:
        return await service.trash_message(
            user_id=user_id,
            connection_id=payload.connection_id,
            message_id=message_id,
        )
    except HTTPException:
        raise
    except Exception as exc:
        _handle_gmail_error(exc)


@router.post(
    "/messages/{message_id}/untrash",
    status_code=status.HTTP_200_OK,
)
async def restore_gmail_message(
    message_id: str,
    payload: GmailMessageActionRequest,
    current_user: CurrentUser,
    db: Session = Depends(get_db),
):
    """Restore a message from Gmail Trash."""
    user_id = _get_user_id(current_user)
    service = GmailService(db)

    try:
        return await service.restore_message(
            user_id=user_id,
            connection_id=payload.connection_id,
            message_id=message_id,
        )
    except HTTPException:
        raise
    except Exception as exc:
        _handle_gmail_error(exc)


@router.post(
    "/messages/{message_id}/archive",
    status_code=status.HTTP_200_OK,
)
async def archive_gmail_message(
    message_id: str,
    payload: GmailMessageActionRequest,
    current_user: CurrentUser,
    db: Session = Depends(get_db),
):
    """Archive a Gmail message by removing its INBOX label."""
    user_id = _get_user_id(current_user)
    service = GmailService(db)

    try:
        return await service.archive_message(
            user_id=user_id,
            connection_id=payload.connection_id,
            message_id=message_id,
        )
    except HTTPException:
        raise
    except Exception as exc:
        _handle_gmail_error(exc)


@router.post(
    "/messages/{message_id}/delete-permanently",
    status_code=status.HTTP_200_OK,
)
async def permanently_delete_gmail_message(
    message_id: str,
    payload: GmailTrashRequest,
    current_user: CurrentUser,
    db: Session = Depends(get_db),
):
    """Permanently delete a message that is already in Gmail Trash."""
    user_id = _get_user_id(current_user)
    service = GmailService(db)

    try:
        return await service.permanently_delete_message(
            user_id=user_id,
            connection_id=payload.connection_id,
            message_id=message_id,
        )
    except HTTPException:
        raise
    except Exception as exc:
        _handle_gmail_error(exc)


@router.post(
    "/messages/{message_id}/reply",
    status_code=status.HTTP_200_OK,
)
async def reply_to_gmail_message(
    message_id: str,
    payload: GmailReplyRequest,
    current_user: CurrentUser,
    db: Session = Depends(get_db),
):
    """Reply to the sender in the original Gmail conversation."""
    user_id = _get_user_id(current_user)
    service = GmailService(db)

    try:
        return await service.reply_message(
            user_id=user_id,
            connection_id=UUID(str(payload.connection_id)),
            message_id=message_id,
            body=payload.body,
            html_body=payload.html_body,
        )
    except HTTPException:
        raise
    except Exception as exc:
        _handle_gmail_error(exc)


@router.post(
    "/messages/{message_id}/reply-all",
    status_code=status.HTTP_200_OK,
)
async def reply_all_to_gmail_message(
    message_id: str,
    payload: GmailReplyRequest,
    current_user: CurrentUser,
    db: Session = Depends(get_db),
):
    """Reply to the sender and other original To/Cc recipients."""
    user_id = _get_user_id(current_user)
    service = GmailService(db)

    try:
        return await service.reply_all_message(
            user_id=user_id,
            connection_id=UUID(str(payload.connection_id)),
            message_id=message_id,
            body=payload.body,
            html_body=payload.html_body,
        )
    except HTTPException:
        raise
    except Exception as exc:
        _handle_gmail_error(exc)


@router.post(
    "/messages/{message_id}/forward",
    status_code=status.HTTP_200_OK,
)
async def forward_gmail_message(
    message_id: str,
    payload: GmailForwardRequest,
    current_user: CurrentUser,
    db: Session = Depends(get_db),
):
    """Forward a message's plain-text content to new recipients."""
    user_id = _get_user_id(current_user)
    service = GmailService(db)

    try:
        return await service.forward_message(
            user_id=user_id,
            connection_id=UUID(str(payload.connection_id)),
            message_id=message_id,
            to=[str(email) for email in payload.to],
            cc=[str(email) for email in payload.cc],
            bcc=[str(email) for email in payload.bcc],
            body=payload.body,
        )
    except HTTPException:
        raise
    except Exception as exc:
        _handle_gmail_error(exc)
