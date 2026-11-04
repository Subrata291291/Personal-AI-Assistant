from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser
from app.db.session import get_db
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
    db: Session = Depends(get_db),
):
    user_id = UUID(str(user["id"]))
    gmail_service = GmailService(db)
    try:
        draft_id = await gmail_service.get_draft_id_by_message_id(
            user_id=user_id,
            connection_id=payload.connection_id,
            message_id=message_id,
        )
        if draft_id is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="This Gmail message is no longer a draft.",
            )
        proposal = service.create_gmail_draft_proposal(
            user_id=user_id,
            connection_id=payload.connection_id,
            parameters={
                "draft_id": draft_id,
                "message_id": message_id,
                "to": [str(address) for address in payload.to],
                "cc": [str(address) for address in payload.cc],
                "bcc": [str(address) for address in payload.bcc],
                "subject": payload.subject,
                "body": payload.body,
            },
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
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return ActionApprovalResponse(proposal=proposal)


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
