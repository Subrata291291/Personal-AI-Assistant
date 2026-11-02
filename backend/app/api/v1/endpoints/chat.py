
from typing import Annotated
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    status,
)
from sqlalchemy.orm import Session
from supabase import Client

from app.api.deps import CurrentUser, get_current_supabase_client
from app.db.session import get_db
from app.memory.memory_pipeline import MemoryPipeline
from app.repositories.chat_request_repository import (
    IdempotencyConflictError,
)
from app.schemas.chat import ChatRequest, ChatResponse
from app.services.chat_service import (
    ChatService,
    ChatRequestInProgressError,
    ChatRequestUncertainError,
    ChatRequestFailedError,
)
from app.services.message_service import MessageService
from app.services.user_settings_service import UserSettingsService


router = APIRouter()


def get_chat_service(
    supabase: Annotated[
        Client,
        Depends(get_current_supabase_client),
    ],
    db: Annotated[
        Session,
        Depends(get_db),
    ],
) -> ChatService:

    message_service = MessageService(supabase)
    user_settings_service = UserSettingsService(db)
    memory_pipeline = MemoryPipeline(db)

    return ChatService(
        message_service=message_service,
        user_settings_service=user_settings_service,
        memory_pipeline=memory_pipeline,
    )


ChatServiceDependency = Annotated[
    ChatService,
    Depends(get_chat_service),
]


@router.post(
    "",
    response_model=ChatResponse,
    status_code=status.HTTP_200_OK,
)
def chat(
    payload: ChatRequest,
    user: CurrentUser,
    service: ChatServiceDependency,
    idempotency_key: Annotated[
        UUID,
        Header(alias="Idempotency-Key"),
    ],
):
    try:
        return service.send_message(
            user_id=UUID(str(user["id"])),
            conversation_id=payload.conversation_id,
            content=payload.content,
            idempotency_key=idempotency_key,
        )

    except LookupError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found.",
        )

    except IdempotencyConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        )

    except (
        ChatRequestInProgressError,
        ChatRequestUncertainError,
        ChatRequestFailedError,
    ) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        )

    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )

    except Exception:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The AI assistant could not process your message.",
        )
