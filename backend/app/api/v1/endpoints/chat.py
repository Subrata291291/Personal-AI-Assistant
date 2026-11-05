
import logging
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
from app.core.error_logging import log_exception_traceback
from app.db.session import get_db
from app.integrations.google.exceptions import GoogleAPIError
from app.llm.fallback import LLMFallbackError
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
from app.services.user_connection_service import UserConnectionService


router = APIRouter()
logger = logging.getLogger(__name__)


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
        user_connection_service=UserConnectionService(db),
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
        user_metadata = user.get("user_metadata")
        return service.send_message(
            user_id=UUID(str(user["id"])),
            conversation_id=payload.conversation_id,
            content=payload.content,
            idempotency_key=idempotency_key,
            gmail_connection_id=payload.gmail_connection_id,
            user_timezone=payload.user_timezone,
            default_calendar_connection_id=(
                user_metadata.get(
                    "default_calendar_connection_id"
                )
                if isinstance(user_metadata, dict)
                else None
            ),
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

    except LLMFallbackError as exc:
        log_exception_traceback(
            logger,
            "Chat request failed after LLM provider fallback was exhausted.",
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "The AI service could not complete your request. "
                "Please try again later."
            ),
        )

    except GoogleAPIError as exc:
        logger.warning(
            "Chat request failed during a Google API call | status=%s",
            exc.status_code,
        )
        if exc.status_code == status.HTTP_401_UNAUTHORIZED:
            response_status = status.HTTP_403_FORBIDDEN
            detail = (
                "Google authorization is no longer valid. Reconnect the "
                "affected Google service in Connected Apps."
            )
        elif exc.status_code == status.HTTP_403_FORBIDDEN:
            response_status = status.HTTP_403_FORBIDDEN
            detail = (
                "Google access was denied. Check the connected account's "
                "permissions in Connected Apps."
            )
        elif exc.status_code == status.HTTP_404_NOT_FOUND:
            response_status = status.HTTP_404_NOT_FOUND
            detail = "The requested Gmail item is no longer available."
        elif (
            exc.retryable
            or exc.status_code == status.HTTP_429_TOO_MANY_REQUESTS
            or exc.status_code >= 500
        ):
            response_status = status.HTTP_503_SERVICE_UNAVAILABLE
            detail = "Google is temporarily unavailable. Please try again later."
        else:
            response_status = status.HTTP_502_BAD_GATEWAY
            detail = "Gmail could not complete the request. Please try again."
        raise HTTPException(
            status_code=response_status,
            detail=detail,
        )

    except Exception as exc:
        log_exception_traceback(
            logger,
            "Unexpected chat request failure.",
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "The assistant encountered an unexpected error. "
                "Please try again later."
            ),
        )
