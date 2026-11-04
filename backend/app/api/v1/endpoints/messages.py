
import logging
from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import ValidationError
from supabase import Client
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, get_current_supabase_client
from app.db.session import get_db
from app.integrations.google.exceptions import GoogleAPIError
from app.schemas.email_draft import (
    EmailDraftUpdate,
    GmailDraftSaveRequest,
    GmailDraftSaveResponse,
    GmailDraftSaveStatus,
    EmailDraftPreview,
)
from app.schemas.message import MessageCreate, MessageResponse
from app.services.gmail_service import GmailService
from app.services.message_service import MessageService


router = APIRouter()
logger = logging.getLogger(__name__)


def get_message_service(
    supabase: Annotated[Client, Depends(get_current_supabase_client)],
) -> MessageService:
    return MessageService(supabase)


MessageServiceDependency = Annotated[
    MessageService,
    Depends(get_message_service),
]


@router.post(
    "",
    response_model=MessageResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_message(
    conversation_id: UUID,
    payload: MessageCreate,
    user: CurrentUser,
    service: MessageServiceDependency,
):
    try:
        return service.create_user_message(
            user_id=user["id"],
            conversation_id=conversation_id,
            content=payload.content,
        )

    except LookupError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found.",
        )

    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )


@router.get(
    "",
    response_model=list[MessageResponse],
)
def get_messages(
    conversation_id: UUID,
    user: CurrentUser,
    service: MessageServiceDependency,
    limit: int = 100,
):
    try:
        return service.get_conversation_messages(
            user_id=user["id"],
            conversation_id=conversation_id,
            limit=limit,
        )

    except LookupError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found.",
        )

    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )


@router.patch(
    "/{message_id}/draft",
    response_model=MessageResponse,
)
def update_email_draft(
    conversation_id: UUID,
    message_id: UUID,
    payload: EmailDraftUpdate,
    user: CurrentUser,
    service: MessageServiceDependency,
):
    try:
        updated = service.update_email_draft(
            user_id=UUID(str(user["id"])),
            conversation_id=conversation_id,
            message_id=message_id,
            email_draft=(
                payload.email_draft.model_dump(mode="json")
                if payload.email_draft is not None
                else None
            ),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Draft preview not found.",
        )
    return updated


@router.post(
    "/{message_id}/draft/save",
    response_model=GmailDraftSaveResponse,
)
async def save_email_draft_to_gmail(
    conversation_id: UUID,
    message_id: UUID,
    payload: GmailDraftSaveRequest,
    user: CurrentUser,
    service: MessageServiceDependency,
    db: Session = Depends(get_db),
):
    user_id = UUID(str(user["id"]))
    gmail_service = GmailService(db)
    try:
        gmail_service.validate_draft_access(
            user_id=user_id,
            connection_id=payload.connection_id,
        )
        stored = service.get_email_draft_for_gmail_save(
            user_id=user_id,
            conversation_id=conversation_id,
            message_id=message_id,
            connection_id=payload.connection_id,
        )
    except PermissionError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    if stored is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation email draft preview not found.",
        )

    _message, draft = stored
    fingerprint = service.email_draft_fingerprint(draft)
    claim = service.claim_gmail_draft_save(
        user_id=user_id,
        conversation_id=conversation_id,
        message_id=message_id,
        connection_id=payload.connection_id,
        operation_id=payload.operation_id,
        fingerprint=fingerprint,
    )

    if claim["status"] == "not_found":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation email draft preview not found.",
        )
    if claim["status"] == "saved":
        saved = claim["record"].get("saved_result") or {}
        saved_preview = claim["record"].get("saved_preview")
        if not saved.get("draft_id") or not saved_preview:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "The saved Gmail draft result is incomplete. "
                    "The operation was not repeated."
                ),
            )
        return GmailDraftSaveResponse(
            state="saved",
            connection_id=UUID(claim["record"]["saved_connection_id"]),
            operation_id=UUID(claim["record"]["saved_operation_id"]),
            draft_id=saved["draft_id"],
            message_id=saved.get("message_id"),
            thread_id=saved.get("thread_id"),
            fingerprint=claim["record"]["saved_fingerprint"],
            saved_preview=saved_preview,
            repeated=True,
        )
    if claim["status"] == "saving":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "A save for this preview is already in progress. "
                "Wait for it to finish before trying again."
            ),
        )
    if claim["status"] == "uncertain":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "The previous Gmail draft save has an uncertain outcome. "
                "It was not retried to avoid creating a duplicate draft."
            ),
        )
    if claim["status"] != "claimed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Another save operation is in progress. Please retry later.",
        )

    try:
        created = await gmail_service.create_draft(
            user_id=user_id,
            connection_id=payload.connection_id,
            draft=draft,
            operation_id=payload.operation_id,
        )
    except GoogleAPIError as exc:
        save_state = (
            "uncertain"
            if exc.status_code >= 500
            else "failed"
        )
        service.finish_gmail_draft_save(
            user_id=user_id,
            conversation_id=conversation_id,
            message_id=message_id,
            operation_id=payload.operation_id,
            state=save_state,
        )
        if save_state == "uncertain":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Gmail's response did not confirm whether the draft was "
                    "created. The operation is blocked from retrying to avoid "
                    "creating a duplicate."
                ),
            ) from exc
        if exc.status_code == 403:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Google denied permission to create this Gmail draft.",
            ) from exc
        if exc.status_code == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="The source Gmail message or thread was not found.",
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Gmail rejected the draft creation request.",
        ) from exc
    except PermissionError as exc:
        service.finish_gmail_draft_save(
            user_id=user_id,
            conversation_id=conversation_id,
            message_id=message_id,
            operation_id=payload.operation_id,
            state="failed",
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        service.finish_gmail_draft_save(
            user_id=user_id,
            conversation_id=conversation_id,
            message_id=message_id,
            operation_id=payload.operation_id,
            state="failed",
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        logger.exception("Gmail draft save failed with an uncertain outcome.")
        service.finish_gmail_draft_save(
            user_id=user_id,
            conversation_id=conversation_id,
            message_id=message_id,
            operation_id=payload.operation_id,
            state="uncertain",
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "The Gmail draft save could not be confirmed. It was not "
                "retried to avoid creating a duplicate."
            ),
        ) from exc

    result = {
        "draft_id": created["draft_id"],
        "message_id": created.get("message_id"),
        "thread_id": created.get("thread_id"),
        "connection_id": str(payload.connection_id),
    }
    try:
        saved_message = service.finish_gmail_draft_save(
            user_id=user_id,
            conversation_id=conversation_id,
            message_id=message_id,
            operation_id=payload.operation_id,
            state="saved",
            result=result,
            saved_preview=draft.model_dump(mode="json"),
        )
    except Exception as exc:
        logger.exception("Gmail draft was created but save metadata could not be persisted.")
        return GmailDraftSaveResponse(
            state="saved",
            connection_id=payload.connection_id,
            operation_id=payload.operation_id,
            draft_id=result["draft_id"],
            message_id=result["message_id"],
            thread_id=result["thread_id"],
            fingerprint=fingerprint,
            saved_preview=draft,
            persistence_warning=(
                "Gmail created the draft, but conversation history could not "
                "be updated. The draft ID is confirmed; check its status "
                "before retrying."
            ),
        )

    if saved_message is None:
        return GmailDraftSaveResponse(
            state="saved",
            connection_id=payload.connection_id,
            operation_id=payload.operation_id,
            draft_id=result["draft_id"],
            message_id=result["message_id"],
            thread_id=result["thread_id"],
            fingerprint=fingerprint,
            saved_preview=draft,
            persistence_warning=(
                "Gmail created the draft, but conversation history did not "
                "confirm the save record. The draft ID is confirmed; check "
                "its status before retrying."
            ),
        )

    return GmailDraftSaveResponse(
        state="saved",
        connection_id=payload.connection_id,
        operation_id=payload.operation_id,
        draft_id=result["draft_id"],
        message_id=result["message_id"],
        thread_id=result["thread_id"],
        fingerprint=fingerprint,
        saved_preview=draft,
    )


@router.get(
    "/{message_id}/draft/save-status",
    response_model=GmailDraftSaveStatus,
)
async def get_email_draft_save_status(
    conversation_id: UUID,
    message_id: UUID,
    connection_id: UUID,
    operation_id: UUID,
    user: CurrentUser,
    service: MessageServiceDependency,
    db: Session = Depends(get_db),
):
    user_id = UUID(str(user["id"]))
    gmail_service = GmailService(db)
    try:
        gmail_service.validate_draft_access(
            user_id=user_id,
            connection_id=connection_id,
        )
        operation = service.get_gmail_draft_save_record(
            user_id=user_id,
            conversation_id=conversation_id,
            message_id=message_id,
            connection_id=connection_id,
            operation_id=operation_id,
        )
    except PermissionError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This Gmail account does not have permission to inspect drafts.",
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc

    state = operation.get("status", "not_found")
    record = operation.get("record") or {}
    if state == "not_found":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The Gmail draft save operation was not found.",
        )

    if state == "saved":
        saved = record.get("saved_result") or {}
        preview = record.get("saved_preview")
        if not saved.get("draft_id"):
            state = "uncertain"
        else:
            try:
                if preview is not None:
                    preview = EmailDraftPreview.model_validate(preview)
            except ValidationError:
                logger.warning(
                    "Saved Gmail draft preview metadata is invalid | message_id=%s",
                    message_id,
                )
                preview = None
            return GmailDraftSaveStatus(
                state="saved",
                connection_id=connection_id,
                operation_id=operation_id,
                draft_id=saved.get("draft_id"),
                message_id=saved.get("message_id"),
                thread_id=saved.get("thread_id"),
                fingerprint=record.get("saved_fingerprint"),
                saved_preview=preview,
                detail="Gmail confirmed this draft was created.",
            )

    if state == "failed":
        return GmailDraftSaveStatus(
            state="failed",
            connection_id=connection_id,
            operation_id=operation_id,
            detail="Gmail rejected the draft creation request; no retry was made.",
        )

    if state == "saving":
        started_at = record.get("started_at")
        try:
            started = datetime.fromisoformat(str(started_at))
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            started = None
        if started and (
            datetime.now(timezone.utc) - started
        ).total_seconds() < 30:
            return GmailDraftSaveStatus(
                state="saving",
                connection_id=connection_id,
                operation_id=operation_id,
                detail="The Gmail draft operation is still being processed.",
            )

    try:
        found = await gmail_service.find_draft_by_operation_id(
            user_id=user_id,
            connection_id=connection_id,
            operation_id=operation_id,
        )
    except PermissionError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This Gmail account does not have permission to inspect drafts.",
        ) from exc
    except (GoogleAPIError, ValueError) as exc:
        logger.warning(
            "Unable to inspect Gmail draft save operation | error_type=%s",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Gmail draft status could not be checked. The outcome remains "
                "uncertain; do not retry the creation."
            ),
        ) from exc

    found_draft = found.get("draft")
    if found_draft is not None:
        save_result = {
            "draft_id": found_draft.get("draft_id"),
            "message_id": found_draft.get("message_id"),
            "thread_id": found_draft.get("thread_id"),
            "connection_id": str(connection_id),
        }
        try:
            stored = service.get_email_draft_for_gmail_save(
                user_id=user_id,
                conversation_id=conversation_id,
                message_id=message_id,
                connection_id=connection_id,
            )
        except Exception as exc:
            logger.exception(
                "Confirmed Gmail draft could not load its conversation preview | message_id=%s",
                message_id,
            )
            stored = None
        preview_data = (
            stored[1].model_dump(mode="json")
            if stored is not None
            else record.get("saved_preview")
        )
        persisted = False
        try:
            persisted = service.finish_gmail_draft_save(
                user_id=user_id,
                conversation_id=conversation_id,
                message_id=message_id,
                operation_id=operation_id,
                state="saved",
                result=save_result,
                saved_preview=preview_data,
                allow_uncertain=True,
            ) is not None
        except Exception:
            logger.exception(
                "Found a Gmail draft but could not persist reconciliation | message_id=%s",
                message_id,
            )
        if not persisted:
            logger.warning(
                "Confirmed Gmail draft result was not persisted | message_id=%s",
                message_id,
            )
        return GmailDraftSaveStatus(
            state="saved",
            connection_id=connection_id,
            operation_id=operation_id,
            draft_id=save_result["draft_id"],
            message_id=save_result["message_id"],
            thread_id=save_result["thread_id"],
            fingerprint=record.get("fingerprint"),
            saved_preview=stored[1] if stored is not None else None,
            detail=(
                "Gmail confirmed the draft by its operation marker. "
                "No new draft was created."
                if persisted
                else
                "Gmail confirmed the draft by its operation marker, but the "
                "conversation history could not be updated. No new draft was created."
            ),
        )

    if state == "saving":
        try:
            service.finish_gmail_draft_save(
                user_id=user_id,
                conversation_id=conversation_id,
                message_id=message_id,
                operation_id=operation_id,
                state="uncertain",
            )
        except Exception:
            logger.exception(
                "Could not persist uncertain Gmail draft status | message_id=%s",
                message_id,
            )

    detail = (
        "No matching draft was found during inspection, but this does not "
        "prove creation failed. The outcome remains uncertain; do not retry."
        if found.get("complete_scan")
        else
        "The draft list was too large to inspect completely. The outcome "
        "remains uncertain; do not retry."
    )
    return GmailDraftSaveStatus(
        state="uncertain",
        connection_id=connection_id,
        operation_id=operation_id,
        detail=detail,
    )
