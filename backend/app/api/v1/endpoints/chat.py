
import json
import logging
from typing import Annotated
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    status,
)
from fastapi.responses import StreamingResponse
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
from app.schemas.chat import (
    ChatRequest,
    ChatResponse,
    ChatRequestStatusResponse,
)
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


def _google_api_http_exception(exc: GoogleAPIError) -> HTTPException:
    if exc.status_code == status.HTTP_401_UNAUTHORIZED:
        return HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Google authorization is no longer valid. Reconnect the "
                "affected Google service in Connected Apps."
            ),
        )
    if exc.status_code == status.HTTP_403_FORBIDDEN:
        return HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Google access was denied. Check the connected account's "
                "permissions in Connected Apps."
            ),
        )
    if exc.status_code == status.HTTP_404_NOT_FOUND:
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The requested Gmail item is no longer available.",
        )
    if (
        exc.retryable
        or exc.status_code == status.HTTP_429_TOO_MANY_REQUESTS
        or exc.status_code >= 500
    ):
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google is temporarily unavailable. Please try again later.",
        )
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail="Gmail could not complete the request. Please try again.",
    )


def _stream_http_exception(exc: Exception) -> HTTPException:
    """Map a pre-stream failure to the same statuses as the blocking endpoint."""
    if isinstance(exc, LookupError):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found.",
        )
    if isinstance(exc, IdempotencyConflictError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        )
    if isinstance(
        exc,
        (
            ChatRequestInProgressError,
            ChatRequestUncertainError,
            ChatRequestFailedError,
        ),
    ):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        )
    if isinstance(exc, ValueError):
        # ValueError text can carry provider/tool payloads; never expose it.
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The request contains an invalid value. Please check your input and try again.",
        )
    if isinstance(exc, LLMFallbackError):
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "The AI service could not complete your request. "
                "Please try again later."
            ),
        )
    if isinstance(exc, GoogleAPIError):
        return _google_api_http_exception(exc)
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail=(
            "The assistant encountered an unexpected error. "
            "Please try again later."
        ),
    )


def _stream_error_message(exc: Exception) -> str:
    """Public, non-sensitive message for an error that interrupts a stream."""
    if isinstance(exc, LookupError):
        return "Conversation not found."
    if isinstance(
        exc,
        (
            IdempotencyConflictError,
            ChatRequestInProgressError,
            ChatRequestUncertainError,
            ChatRequestFailedError,
        ),
    ):
        # Conflict messages carry user-facing idempotency/retry guidance.
        return str(exc)
    if isinstance(exc, ValueError):
        # ValueError text can carry provider/tool payloads; never echo it.
        return "The request contains an invalid value. Please check your input and try again."
    if isinstance(exc, LLMFallbackError):
        return (
            "The AI service could not complete your request. "
            "Please try again later."
        )
    if isinstance(exc, GoogleAPIError):
        return (
            "Google service temporarily unavailable. "
            "Please try again later."
        )
    return (
        "The assistant is temporarily unavailable. Please try again."
    )


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
            default_contacts_connection_id=(
                user_metadata.get(
                    "default_contacts_connection_id"
                )
                if isinstance(user_metadata, dict)
                else None
            ),
            default_drive_connection_id=(
                user_metadata.get(
                    "default_drive_connection_id"
                )
                if isinstance(user_metadata, dict)
                else None
            ),
            gmail_enabled=(
                user_metadata.get("gmail_enabled") is not False
                if isinstance(user_metadata, dict)
                else True
            ),
            calendar_enabled=(
                user_metadata.get("calendar_enabled") is not False
                if isinstance(user_metadata, dict)
                else True
            ),
            contacts_enabled=(
                user_metadata.get("contacts_enabled") is not False
                if isinstance(user_metadata, dict)
                else True
            ),
            drive_enabled=(
                user_metadata.get("drive_enabled") is not False
                if isinstance(user_metadata, dict)
                else True
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


@router.post("/stream")
def chat_stream(
    payload: ChatRequest,
    user: CurrentUser,
    service: ChatServiceDependency,
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
):
    """SSE chat with genuine provider token streaming.

    Tokens are forwarded from the LLM provider through the LangGraph custom
    stream to this response as they are generated. Tool execution produces
    short status events; raw tool payloads are never forwarded.
    """
    user_metadata = user.get("user_metadata") or {}

    def _sse(event: dict) -> str:
        return f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"

    def _done_payload(response: dict) -> dict:
        assistant_msg = response.get("assistant_message") or {}
        user_msg = response.get("user_message") or {}
        conversation = response.get("conversation_id")
        payload = {
            "type": "done",
            "message_id": assistant_msg.get("id"),
            "user_message_id": user_msg.get("id"),
            "conversation_id": str(conversation) if conversation else None,
            "request_id": str(idempotency_key),
            "provider": response.get("provider"),
            "model": response.get("model"),
        }
        # Surface explicit completeness state to the streaming client so it
        # cannot present a bounded/partial answer as complete.
        assistant_metadata = assistant_msg.get("metadata") or {}
        completion = assistant_metadata.get("response_completion")
        if isinstance(completion, dict):
            payload["completion"] = completion
        return payload

    events = None
    try:
        events = service.stream_message(
            user_id=UUID(str(user["id"])),
            conversation_id=payload.conversation_id,
            content=payload.content,
            idempotency_key=idempotency_key,
            gmail_connection_id=payload.gmail_connection_id,
            user_timezone=payload.user_timezone,
            default_calendar_connection_id=user_metadata.get("default_calendar_connection_id"),
            default_contacts_connection_id=user_metadata.get("default_contacts_connection_id"),
            default_drive_connection_id=user_metadata.get("default_drive_connection_id"),
            gmail_enabled=(user_metadata.get("gmail_enabled") is not False),
            calendar_enabled=(user_metadata.get("calendar_enabled") is not False),
            contacts_enabled=(user_metadata.get("contacts_enabled") is not False),
            drive_enabled=(user_metadata.get("drive_enabled") is not False),
        )
        first_event = next(events)
    except StopIteration:
        first_event = None
    except Exception as exc:
        log_exception_traceback(
            logger,
            "Streaming chat failed before streaming started.",
            exc,
        )
        raise _stream_http_exception(exc)

    def event_generator():
        content_started = False
        try:
            if first_event is not None:
                if first_event.get("type") == "done":
                    yield _sse(_done_payload(first_event["response"]))
                    return
                if first_event.get("type") in {"token", "replace"}:
                    content_started = True
                yield _sse(first_event)

            for event in events:
                event_type = event.get("type")
                if event_type == "done":
                    yield _sse(_done_payload(event["response"]))
                    return
                if event_type not in {"token", "status", "replace"}:
                    continue
                if event_type in {"token", "replace"}:
                    content_started = True
                yield _sse(event)
        except Exception as exc:
            log_exception_traceback(
                logger,
                "Streaming chat failed.",
                exc,
            )
            yield _sse({
                "type": "error",
                "message": _stream_error_message(exc),
                "partial": content_started,
            })
        finally:
            close = getattr(events, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:
                    logger.debug(
                        "Streaming chat generator close failed",
                        exc_info=True,
                    )

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get(
    "/requests/{idempotency_key}",
    response_model=ChatRequestStatusResponse,
    status_code=status.HTTP_200_OK,
)
def get_chat_request_status(
    idempotency_key: UUID,
    conversation_id: Annotated[UUID, Query(...)],
    user: CurrentUser,
    service: ChatServiceDependency,
):
    try:
        return service.get_chat_request_status(
            user_id=UUID(str(user["id"])),
            conversation_id=conversation_id,
            idempotency_key=idempotency_key,
        )
    except LookupError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Chat request not found.",
        )
    except Exception as exc:
        log_exception_traceback(
            logger,
            "Failed to retrieve chat request status.",
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to retrieve chat request status.",
        )
