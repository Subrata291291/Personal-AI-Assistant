import uuid
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import select, update, desc
from sqlalchemy.orm import Session

from app.db.models.memory import Memory
from app.db.models.memory_review import MemoryReview


ReviewStatus = Literal["pending", "accepted", "rejected"]


class MemoryReviewRepository:
    """
    Database repository for user-specific memory reviews.

    Every operation explicitly scopes records by user_id
    because the backend database role bypasses RLS.
    """

    def __init__(self, session: Session):
        self.session = session

    def create_review(
        self,
        user_id: uuid.UUID,
        existing_content: str,
        proposed_content: str,
        reason: str,
        confidence: float,
        existing_memory_id: uuid.UUID | None = None,
    ) -> MemoryReview:

        # Verify that the existing memory belongs to this user.
        if existing_memory_id is not None:
            statement = select(Memory.id).where(
                Memory.id == existing_memory_id,
                Memory.user_id == user_id,
            )

            if self.session.scalar(statement) is None:
                raise ValueError(
                    "Existing memory does not belong to this user."
                )

        review = MemoryReview(
            user_id=user_id,
            existing_memory_id=existing_memory_id,
            existing_content=existing_content,
            proposed_content=proposed_content,
            reason=reason,
            confidence=confidence,
            status="pending",
        )

        self.session.add(review)
        self.session.flush()

        return review

    def get_review(
        self,
        user_id: uuid.UUID,
        review_id: uuid.UUID,
    ) -> MemoryReview | None:

        statement = select(MemoryReview).where(
            MemoryReview.id == review_id,
            MemoryReview.user_id == user_id,
        )

        return self.session.scalar(statement)

    def list_reviews(
        self,
        user_id: uuid.UUID,
        status: ReviewStatus | None = "pending",
        limit: int = 50,
        offset: int = 0,
    ) -> list[MemoryReview]:

        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100.")

        if offset < 0:
            raise ValueError("offset cannot be negative.")

        statement = select(MemoryReview).where(
            MemoryReview.user_id == user_id,
        )

        if status is not None:
            statement = statement.where(
                MemoryReview.status == status,
            )

        statement = (
            statement
            .order_by(desc(MemoryReview.created_at))
            .limit(limit)
            .offset(offset)
        )

        return list(self.session.scalars(statement).all())

    def resolve_review(
        self,
        user_id: uuid.UUID,
        review_id: uuid.UUID,
        status: Literal["accepted", "rejected"],
    ) -> MemoryReview | None:

        if status not in ("accepted", "rejected"):
            raise ValueError(
                "Review status must be accepted or rejected."
            )

        statement = (
            update(MemoryReview)
            .where(
                MemoryReview.id == review_id,
                MemoryReview.user_id == user_id,
                MemoryReview.status == "pending",
            )
            .values(
                status=status,
                resolved_at=datetime.now(timezone.utc),
            )
            .returning(MemoryReview)
        )

        return self.session.scalar(statement)