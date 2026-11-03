
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from supabase import Client

from app.api.deps import CurrentUser, get_current_supabase_client
from app.schemas.email_draft import EmailDraftUpdate
from app.schemas.message import MessageCreate, MessageResponse
from app.services.message_service import MessageService


router = APIRouter()


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
