from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser
from app.db.session import get_db
from app.schemas.memory_review import MemoryReviewResponse
from app.services.memory_review_service import MemoryReviewService


router = APIRouter()


def get_memory_review_service(
    db: Annotated[Session, Depends(get_db)],
) -> MemoryReviewService:
    return MemoryReviewService(db)


MemoryReviewServiceDependency = Annotated[
    MemoryReviewService,
    Depends(get_memory_review_service),
]


@router.get(
    "",
    response_model=list[MemoryReviewResponse],
)
def list_memory_reviews(
    user: CurrentUser,
    service: MemoryReviewServiceDependency,
    status_filter: str | None = Query(default="pending"),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    if status_filter not in (None, "pending", "accepted", "rejected"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid review status.",
        )

    try:
        return service.list_reviews(
            user_id=UUID(str(user["id"])),
            status=status_filter,
            limit=limit,
            offset=offset,
        )

    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )


@router.post(
    "/{review_id}/accept",
    response_model=MemoryReviewResponse,
)
def accept_memory_review(
    review_id: UUID,
    user: CurrentUser,
    service: MemoryReviewServiceDependency,
):
    try:
        return service.accept_review(
            user_id=UUID(str(user["id"])),
            review_id=review_id,
        )

    except ValueError as exc:
        message = str(exc)

        if "not found" in message.lower():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=message,
            )

        if "already been resolved" in message.lower():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=message,
            )

        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=message,
        )


@router.post(
    "/{review_id}/reject",
    response_model=MemoryReviewResponse,
)
def reject_memory_review(
    review_id: UUID,
    user: CurrentUser,
    service: MemoryReviewServiceDependency,
):
    try:
        return service.reject_review(
            user_id=UUID(str(user["id"])),
            review_id=review_id,
        )

    except ValueError as exc:
        message = str(exc)

        if "not found" in message.lower():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=message,
            )

        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=message,
        )