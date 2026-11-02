import uuid
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy.orm import Session

from app.db.models.memory import Memory
from app.db.models.memory_review import MemoryReview
from app.memory.embedding_service import EmbeddingService
from app.repositories.memory_repository import MemoryRepository
from app.repositories.memory_review_repository import (
    MemoryReviewRepository,
)


class MemoryReviewService:
    """
    Application service for accepting and rejecting memory reviews.

    Accept operation:
    1. Verify review ownership and pending status.
    2. Verify existing memory ownership.
    3. Check whether the existing memory has changed.
    4. Generate a new embedding.
    5. Update memory and resolve review in one transaction.
    """

    def __init__(self, session: Session):
        self.session = session
        self.memory_repository = MemoryRepository(session)
        self.review_repository = MemoryReviewRepository(session)

    def list_reviews(
        self,
        user_id: uuid.UUID,
        status: Literal["pending", "accepted", "rejected"] | None = "pending",
        limit: int = 50,
        offset: int = 0,
    ) -> list[MemoryReview]:

        return self.review_repository.list_reviews(
            user_id=user_id,
            status=status,
            limit=limit,
            offset=offset,
        )

    def get_review(
        self,
        user_id: uuid.UUID,
        review_id: uuid.UUID,
    ) -> MemoryReview | None:

        return self.review_repository.get_review(
            user_id=user_id,
            review_id=review_id,
        )

    def accept_review(
        self,
        user_id: uuid.UUID,
        review_id: uuid.UUID,
    ) -> MemoryReview:

        try:
            review = self.review_repository.get_review(
                user_id=user_id,
                review_id=review_id,
            )

            if review is None:
                raise ValueError("Memory review not found.")

            if review.status != "pending":
                raise ValueError("This review has already been resolved.")

            if review.existing_memory_id is None:
                raise ValueError(
                    "The original memory no longer exists. "
                    "This review cannot be accepted."
                )

            memory = self.memory_repository.get_memory(
                user_id=user_id,
                memory_id=review.existing_memory_id,
            )

            if memory is None:
                raise ValueError("Existing memory not found.")

            # Prevent overwriting a memory that changed after review creation.
            if memory.content != review.existing_content:
                raise ValueError(
                    "The existing memory has changed since this review "
                    "was created. Please review the latest memory."
                )

            proposed_content = review.proposed_content.strip()

            if not proposed_content:
                raise ValueError("Proposed memory content is empty.")

            # Generate embedding before changing database records.
            embedding = EmbeddingService().embed_text(
                proposed_content
            )

            if len(embedding) != 768:
                raise ValueError(
                    "Embedding must contain exactly 768 dimensions."
                )

            updated_memory = self.memory_repository.update_memory(
                user_id=user_id,
                memory_id=memory.id,
                content=proposed_content,
                embedding=embedding,
                confidence=review.confidence,
            )

            if updated_memory is None:
                raise ValueError("Unable to update the existing memory.")

            resolved_review = self.review_repository.resolve_review(
                user_id=user_id,
                review_id=review_id,
                status="accepted",
            )

            if resolved_review is None:
                raise ValueError(
                    "Review is no longer pending or cannot be resolved."
                )

            # Both database changes are committed together.
            self.session.commit()

            self.session.refresh(resolved_review)

            return resolved_review

        except Exception:
            self.session.rollback()
            raise

    def reject_review(
        self,
        user_id: uuid.UUID,
        review_id: uuid.UUID,
    ) -> MemoryReview:

        try:
            resolved_review = self.review_repository.resolve_review(
                user_id=user_id,
                review_id=review_id,
                status="rejected",
            )

            if resolved_review is None:
                raise ValueError(
                    "Pending memory review not found."
                )

            self.session.commit()

            self.session.refresh(resolved_review)

            return resolved_review

        except Exception:
            self.session.rollback()
            raise