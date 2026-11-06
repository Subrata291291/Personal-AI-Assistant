from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, is_integration_enabled
from app.core.logging import get_logger
from app.core.request_metrics import request_id
from app.db.session import get_db
from app.integrations.google.exceptions import GoogleAPIError
from app.schemas.action_approval import (
    ActionApprovalProposal,
    ActionApprovalResponse,
    ApprovalProposalCreate,
    GmailDraftSendApprovalRequest,
)
from app.services.gmail_service import GmailService
from app.services.action_approval_service import (
    ActionApprovalService,
    ApprovalConflictError,
    ApprovalExpiredError,
)
from app.services.gmail_action_executor import (
    ExecutionStatePersistenceError,
    GmailActionExecutor,
)
from app.services.calendar_action_executor import (
    CalendarActionExecutor,
    CalendarExecutionStatePersistenceError,
)


router = APIRouter()
logger = get_logger(__name__)


def get_action_approval_service(
    db: Session = Depends(get_db),
) -> ActionApprovalService:
    return ActionApprovalService(db)


ActionApprovalServiceDependency = Annotated[
    ActionApprovalService,
    Depends(get_action_approval_service),
]


@router.post(
    "/gmail-drafts/{message_id}",
    response_model=ActionApprovalResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_gmail_draft_send_approval(
    message_id: str,
    payload: GmailDraftSendApprovalRequest,
    user: CurrentUser,
    service: ActionApprovalServiceDependency,
    idempotency_key: Annotated[
        UUID,
        Header(alias="Idempotency-Key"),
    ],
    db: Session = Depends(get_db),
):
    if not is_integration_enabled(user, "gmail"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Gmail is turned off in Connected Apps.",
        )
    user_id = UUID(str(user["id"]))
    approval_id = ActionApprovalService._gmail_draft_proposal_id(
        user_id,
        idempotency_key,
    )
    logger.info(
        "gmail_draft_approval_creation_started | request_id=%s "
        "| user_id=%s | connection_id=%s | idempotency_key=%s "
        "| approval_id=%s",
        request_id(),
        user_id,
        payload.connection_id,
        idempotency_key,
        approval_id,
    )
    request_parameters = {
        "message_id": message_id,
        "to": [str(address) for address in payload.to],
        "cc": [str(address) for address in payload.cc],
        "bcc": [str(address) for address in payload.bcc],
        "subject": payload.subject.strip(),
        "body": payload.body,
    }
    try:
        existing = service.get_gmail_draft_proposal(
            user_id=user_id,
            idempotency_key=idempotency_key,
        )
    except Exception as exc:
        logger.error(
            "gmail_draft_approval_status_lookup_failed | request_id=%s "
            "| user_id=%s | idempotency_key=%s | approval_id=%s "
            "| lookup_result=error | http_status=503 | error_type=%s",
            request_id(),
            user_id,
            idempotency_key,
            approval_id,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Approval status could not be checked. Do not retry until "
                "the request is reconciled."
            ),
        ) from exc
    if existing is not None:
        logger.info(
            "gmail_draft_approval_status_lookup_completed | request_id=%s "
            "| user_id=%s | idempotency_key=%s | approval_id=%s "
            "| lookup_result=found | http_status=200",
            request_id(),
            user_id,
            idempotency_key,
            existing.proposal_id,
        )
        try:
            service.validate_gmail_draft_request_match(
                proposal=existing,
                connection_id=payload.connection_id,
                message_id=message_id,
                parameters=request_parameters,
            )
        except ApprovalConflictError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
        return ActionApprovalResponse(proposal=existing)
    logger.info(
        "gmail_draft_approval_status_lookup_completed | request_id=%s "
        "| user_id=%s | idempotency_key=%s | approval_id=%s "
        "| lookup_result=missing | http_status=404",
        request_id(),
        user_id,
        idempotency_key,
        approval_id,
    )

    gmail_service = GmailService(db)
    try:
        draft_id = await gmail_service.get_draft_id_by_message_id(
            user_id=user_id,
            connection_id=payload.connection_id,
            message_id=message_id,
        )
    except GoogleAPIError as exc:
        logger.warning(
            "gmail_draft_approval_draft_lookup_failed | request_id=%s "
            "| google_status=%s",
            request_id(),
            exc.status_code,
        )
        if exc.status_code == status.HTTP_401_UNAUTHORIZED:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "Google authorization is no longer valid. Reconnect "
                    "Gmail in Connected Apps, then try again. No approval "
                    "proposal was created."
                ),
            ) from exc
        if exc.retryable or exc.status_code == status.HTTP_429_TOO_MANY_REQUESTS:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "Google is temporarily unavailable. No approval "
                    "proposal was created."
                ),
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                "Gmail could not confirm the draft before creating an "
                "approval proposal. No proposal was created."
            ),
        ) from exc
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
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
    except Exception as exc:
        logger.error(
            "gmail_draft_approval_draft_lookup_failed | request_id=%s "
            "| error_type=%s",
            request_id(),
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Gmail draft status could not be confirmed. No approval "
                "proposal was created."
            ),
        ) from exc
    try:
        if draft_id is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="This Gmail message is no longer a draft.",
            )
        proposal = service.create_gmail_draft_proposal(
            user_id=user_id,
            connection_id=payload.connection_id,
            message_id=message_id,
            idempotency_key=idempotency_key,
            parameters={
                **request_parameters,
                "draft_id": draft_id,
            },
        )
    except HTTPException:
        raise
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
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
    except ApprovalConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        logger.error(
            "gmail_draft_approval_creation_failed | request_id=%s "
            "| user_id=%s | connection_id=%s | idempotency_key=%s "
            "| approval_id=%s | database_transaction=not_confirmed "
            "| http_status=503 | error_type=%s",
            request_id(),
            user_id,
            payload.connection_id,
            idempotency_key,
            approval_id,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Approval creation could not be confirmed. Check approval "
                "status before retrying."
            ),
        ) from exc
    logger.info(
        "gmail_draft_approval_creation_confirmed | request_id=%s "
        "| user_id=%s | connection_id=%s | idempotency_key=%s "
        "| approval_id=%s | database_transaction=committed "
        "| http_status=201",
        request_id(),
        user_id,
        payload.connection_id,
        idempotency_key,
        proposal.proposal_id,
    )
    return ActionApprovalResponse(proposal=proposal)


@router.get(
    "/gmail-drafts/approval/{idempotency_key}",
    response_model=ActionApprovalProposal,
)
def get_gmail_draft_send_approval_status(
    idempotency_key: UUID,
    user: CurrentUser,
    service: ActionApprovalServiceDependency,
):
    user_id = UUID(str(user["id"]))
    proposal_id = ActionApprovalService._gmail_draft_proposal_id(
        user_id,
        idempotency_key,
    )
    try:
        proposal = service.get_gmail_draft_proposal(
            user_id=user_id,
            idempotency_key=idempotency_key,
        )
    except Exception as exc:
        logger.error(
            "gmail_draft_approval_status_lookup_failed | request_id=%s "
            "| user_id=%s | idempotency_key=%s | approval_id=%s "
            "| lookup_result=error | http_status=503 | error_type=%s",
            request_id(),
            user_id,
            idempotency_key,
            proposal_id,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Approval status could not be checked. Do not retry until "
                "the request is reconciled."
            ),
        ) from exc
    if proposal is None:
        logger.info(
            "gmail_draft_approval_status_lookup_completed | request_id=%s "
            "| user_id=%s | idempotency_key=%s | approval_id=%s "
            "| lookup_result=missing | http_status=404",
            request_id(),
            user_id,
            idempotency_key,
            proposal_id,
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No approval proposal was created for this request.",
        )
    logger.info(
        "gmail_draft_approval_status_lookup_completed | request_id=%s "
        "| user_id=%s | idempotency_key=%s | approval_id=%s "
        "| lookup_result=found | http_status=200",
        request_id(),
        user_id,
        idempotency_key,
        proposal.proposal_id,
    )
    return proposal


@router.post(
    "",
    response_model=ActionApprovalResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_action_approval(
    payload: ApprovalProposalCreate,
    user: CurrentUser,
    service: ActionApprovalServiceDependency,
):
    integration = (
        "gmail"
        if payload.action_type.startswith("gmail.")
        else "calendar"
        if payload.action_type.startswith("calendar.")
        else None
    )
    if integration and not is_integration_enabled(user, integration):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"{integration.title()} is turned off in Connected Apps.",
        )
    try:
        proposal = service.create_proposal(
            user_id=UUID(str(user["id"])),
            payload=payload,
        )
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except PermissionError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    return ActionApprovalResponse(proposal=proposal)


@router.get(
    "",
    response_model=list[ActionApprovalProposal],
)
def list_pending_action_approvals(
    user: CurrentUser,
    service: ActionApprovalServiceDependency,
    conversation_id: UUID | None = None,
):
    return service.list_pending(
        user_id=UUID(str(user["id"])),
        conversation_id=conversation_id,
    )


@router.get(
    "/{proposal_id}",
    response_model=ActionApprovalProposal,
)
async def get_action_approval(
    proposal_id: UUID,
    user: CurrentUser,
    service: ActionApprovalServiceDependency,
    db: Session = Depends(get_db),
):
    try:
        proposal = service.get_proposal(
            user_id=UUID(str(user["id"])),
            proposal_id=proposal_id,
        )
        if (
            proposal.action_type == "gmail.send_draft"
            and proposal.status == "execution_uncertain"
        ):
            executor = GmailActionExecutor(
                db=db,
                approval_service=service,
            )
            proposal = await executor.reconcile(
                user_id=UUID(str(user["id"])),
                proposal_id=proposal_id,
            )
        elif (
            proposal.action_type in CalendarActionExecutor.SUPPORTED_ACTION_TYPES
            and proposal.status in {"execution_uncertain", "executing"}
            and is_integration_enabled(user, "calendar")
        ):
            proposal = await CalendarActionExecutor(
                db=db,
                approval_service=service,
            ).reconcile(
                user_id=UUID(str(user["id"])),
                proposal_id=proposal_id,
            )
        return proposal
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except ExecutionStatePersistenceError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except CalendarExecutionStatePersistenceError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc


@router.post(
    "/{proposal_id}/approve",
    response_model=ActionApprovalResponse,
)
def approve_action(
    proposal_id: UUID,
    user: CurrentUser,
    service: ActionApprovalServiceDependency,
):
    try:
        proposal = service.approve(
            user_id=UUID(str(user["id"])),
            proposal_id=proposal_id,
        )
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except PermissionError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    except ApprovalExpiredError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except ApprovalConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    return ActionApprovalResponse(proposal=proposal)


@router.post(
    "/{proposal_id}/execute",
    response_model=ActionApprovalResponse,
)
async def execute_action(
    proposal_id: UUID,
    user: CurrentUser,
    service: ActionApprovalServiceDependency,
):
    try:
        user_id = UUID(str(user["id"]))
        current = service.get_proposal(
            user_id=user_id,
            proposal_id=proposal_id,
        )
        integration = (
            "gmail"
            if current.action_type.startswith("gmail.")
            else "calendar"
            if current.action_type.startswith("calendar.")
            else None
        )
        if integration and not is_integration_enabled(user, integration):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"{integration.title()} is turned off in Connected Apps."
                ),
            )
        if current.action_type in GmailActionExecutor.SUPPORTED_ACTION_TYPES:
            proposal = await GmailActionExecutor(
                db=service.db,
                approval_service=service,
            ).execute(
                user_id=user_id,
                proposal_id=proposal_id,
            )
        elif current.action_type in CalendarActionExecutor.SUPPORTED_ACTION_TYPES:
            proposal = await CalendarActionExecutor(
                db=service.db,
                approval_service=service,
            ).execute(
                user_id=user_id,
                proposal_id=proposal_id,
            )
        else:
            raise PermissionError(
                "This action type cannot be executed."
            )
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except PermissionError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    except ApprovalExpiredError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except ApprovalConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except ExecutionStatePersistenceError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except CalendarExecutionStatePersistenceError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except GoogleAPIError as exc:
        if exc.status_code in {401, 403}:
            response_status = status.HTTP_403_FORBIDDEN
            detail = (
                "The selected Google account is no longer authorized for "
                "this Calendar."
            )
        elif exc.status_code == 404:
            response_status = status.HTTP_404_NOT_FOUND
            detail = "The selected Google Calendar is unavailable."
        elif exc.status_code == 429 or exc.status_code >= 500:
            response_status = status.HTTP_503_SERVICE_UNAVAILABLE
            detail = (
                "Google Calendar is temporarily unavailable. The event "
                "request was not automatically retried."
            )
        else:
            response_status = status.HTTP_502_BAD_GATEWAY
            detail = "Google Calendar could not process the request."
        raise HTTPException(
            status_code=response_status,
            detail=detail,
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return ActionApprovalResponse(proposal=proposal)


@router.post(
    "/{proposal_id}/reject",
    response_model=ActionApprovalResponse,
)
def reject_action(
    proposal_id: UUID,
    user: CurrentUser,
    service: ActionApprovalServiceDependency,
):
    try:
        proposal = service.reject(
            user_id=UUID(str(user["id"])),
            proposal_id=proposal_id,
        )
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except ApprovalExpiredError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except ApprovalConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    return ActionApprovalResponse(proposal=proposal)
