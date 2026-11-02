
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from supabase import Client

from app.api.deps import CurrentUser, get_current_supabase_client
from app.schemas.conversation import (
    ConversationCreate,
    ConversationUpdate,
    ConversationResponse,
)
from app.services.conversation_service import ConversationService


router = APIRouter()


def get_conversation_service(
    supabase: Annotated[Client, Depends(get_current_supabase_client)],
) -> ConversationService:
    return ConversationService(supabase)


ConversationServiceDependency = Annotated[
    ConversationService,
    Depends(get_conversation_service),
]


@router.post(
    "",
    response_model=ConversationResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_conversation(
    payload: ConversationCreate,
    user: CurrentUser,
    service: ConversationServiceDependency,
):
    return service.create_conversation(
        user_id=user["id"],
        title=payload.title,
    )


@router.get(
    "",
    response_model=list[ConversationResponse],
)
def list_conversations(
    user: CurrentUser,
    service: ConversationServiceDependency,
):
    return service.list_conversations(user_id=user["id"])


@router.get(
    "/{conversation_id}",
    response_model=ConversationResponse,
)
def get_conversation(
    conversation_id: UUID,
    user: CurrentUser,
    service: ConversationServiceDependency,
):
    conversation = service.get_conversation(
        user_id=user["id"],
        conversation_id=conversation_id,
    )

    if conversation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found.",
        )

    return conversation


@router.patch(
    "/{conversation_id}",
    response_model=ConversationResponse,
)
def update_conversation(
    conversation_id: UUID,
    payload: ConversationUpdate,
    user: CurrentUser,
    service: ConversationServiceDependency,
):
    conversation = service.update_conversation(
        user_id=user["id"],
        conversation_id=conversation_id,
        title=payload.title,
    )

    if conversation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found.",
        )

    return conversation


@router.patch(
    "/{conversation_id}/archive",
    response_model=ConversationResponse,
)
def archive_conversation(
    conversation_id: UUID,
    user: CurrentUser,
    service: ConversationServiceDependency,
):
    conversation = service.archive_conversation(
        user_id=user["id"],
        conversation_id=conversation_id,
    )

    if conversation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found.",
        )

    return conversation


@router.delete(
    "/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_conversation(
    conversation_id: UUID,
    user: CurrentUser,
    service: ConversationServiceDependency,
):
    deleted = service.delete_conversation(
        user_id=user["id"],
        conversation_id=conversation_id,
    )

    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found.",
        )
