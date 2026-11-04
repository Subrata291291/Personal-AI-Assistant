from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser
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
    user_id = UUID(str(user["id"]))
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
            "| error_type=%s",
            request_id(),
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
            "| error_type=%s",
            request_id(),
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Approval creation could not be confirmed. Check approval "
                "status before retrying."
            ),
        ) from exc
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
    try:
        proposal = service.get_gmail_draft_proposal(
            user_id=UUID(str(user["id"])),
            idempotency_key=idempotency_key,
        )
    except Exception as exc:
        logger.error(
            "gmail_draft_approval_status_lookup_failed | request_id=%s "
            "| error_type=%s",
            request_id(),
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
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No approval proposal was created for this request.",
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
    executor = GmailActionExecutor(
        db=service.db,
        approval_service=service,
    )
    try:
        proposal = await executor.execute(
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
    except ExecutionStatePersistenceError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
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
