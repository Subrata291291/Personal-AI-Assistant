
import uuid
import re
from typing import Literal

from pydantic import BaseModel, Field

from sqlalchemy.orm import Session

from app.memory.classifier.memory_classifier import MemoryClassifier
from app.memory.classifier.memory_policy import MemoryPolicy
from app.memory.deduplication.duplicate_detector import DuplicateDetector
from app.memory.deduplication.memory_conflict_resolver import (
    MemoryConflictResolver,
)
from app.repositories.memory_review_repository import (
    MemoryReviewRepository,
)
from app.services.memory_service import MemoryService
from app.db.models.memory_review import MemoryReview


_MEMORY_OPT_OUT = re.compile(
    r"\b(?:don't|do not|never)\s+(?:remember|store|save|retain)\b"
    r"|\bforget\s+(?:this|that|it|the details|this information|that information)\b"
    r"|\bdelete\s+(?:this|that|the)\s+(?:memory|information)\b",
    re.IGNORECASE,
)
_SENSITIVE_CONTENT_PATTERNS = (
    re.compile(
        r"\b(?:password|passphrase|passcode|pin|api[\s_-]*key|"
        r"access[\s_-]*token|refresh[\s_-]*token|oauth[\s_-]*token|"
        r"client[\s_-]*secret|secret[\s_-]*key|private[\s_-]*key|credential)"
        r"\b\s*(?:is|:|=)\s*['\"]?\S{3,}",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bBearer\s+[A-Za-z0-9._~+/-]{12,}={0,2}"
        r"|\b(?:sk-[A-Za-z0-9_-]{16,}|AIza[A-Za-z0-9_-]{25,}|"
        r"gh[pousr]_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{15,})",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:credit|debit)\s*card(?:\s*(?:number|no\.?))?\s*"
        r"(?:is|:|=)?\s*(?:\d[ -]?){13,19}\d?"
        r"|\b(?:bank account|routing number|social security number|ssn)"
        r"\s*(?:is|:|=)\s*[\w -]{4,}",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:my\s+)?(?:diagnosis|medical condition|health condition)"
        r"\s+(?:is|:)\s+\S+",
        re.IGNORECASE,
    ),
)


def _contains_sensitive_content(text: str) -> bool:
    return any(pattern.search(text) for pattern in _SENSITIVE_CONTENT_PATTERNS)


MemoryStatus = Literal[
    "stored",
    "updated",
    "rejected",
    "duplicate",
    "review_required",
]


class MemoryItemResult(BaseModel):
    status: MemoryStatus
    reason: str

    memory_id: uuid.UUID | None = None
    memory_type: str | None = None
    content: str | None = None
    importance: int | None = None
    confidence: float | None = None


class MemoryPipelineResult(BaseModel):
    status: Literal[
        "stored",
        "updated",
        "rejected",
        "duplicate",
        "review_required",
        "disabled",
    ]

    reason: str

    memory_id: uuid.UUID | None = None
    memory_type: str | None = None
    confidence: float | None = None

    items: list[MemoryItemResult] = Field(default_factory=list)


class MemoryPipeline:

    def __init__(self, session: Session):

        self.classifier = MemoryClassifier()
        self.policy = MemoryPolicy()
        self.conflict_resolver = MemoryConflictResolver()

        self.session = session

        self.memory_service = MemoryService(session)
        self.duplicate_detector = DuplicateDetector(session)
        self.review_repository = MemoryReviewRepository(session)

    def _create_pending_review(
        self,
        user_id: uuid.UUID,
        existing_memory_id: uuid.UUID,
        existing_content: str,
        proposed_content: str,
        reason: str,
        confidence: float,
    ) -> MemoryReview:

        try:
            review = self.review_repository.create_review(
                user_id=user_id,
                existing_memory_id=existing_memory_id,
                existing_content=existing_content,
                proposed_content=proposed_content,
                reason=reason,
                confidence=confidence,
            )

            self.session.commit()
            self.session.refresh(review)

            return review

        except Exception:
            self.session.rollback()
            raise

    def process(
        self,
        user_id: uuid.UUID,
        content: str,
        memory_enabled: bool = False,
        source_conversation_id: uuid.UUID | None = None,
    ) -> MemoryPipelineResult:

        # Step 1: Respect user memory preference.
        if not memory_enabled:
            return MemoryPipelineResult(
                status="disabled",
                reason="Automatic memory storage is disabled.",
            )

        if not content or not content.strip():
            return MemoryPipelineResult(
                status="rejected",
                reason="Memory content cannot be empty.",
            )

        if _MEMORY_OPT_OUT.search(content):
            return MemoryPipelineResult(
                status="rejected",
                reason="The user asked not to store this information.",
            )

        if _contains_sensitive_content(content):
            return MemoryPipelineResult(
                status="rejected",
                reason="Sensitive information is not stored automatically.",
            )

        # Step 2: Extract memories.
        classification = self.classifier.classify(content)

        if not classification.memories:
            return MemoryPipelineResult(
                status="rejected",
                reason="No meaningful memories were identified.",
            )

        item_results: list[MemoryItemResult] = []

        # Step 3: Process each extracted memory.
        for extracted_memory in classification.memories:

            memory_content = extracted_memory.content

            # Step 3A: Policy validation.
            if _contains_sensitive_content(memory_content):
                item_results.append(
                    MemoryItemResult(
                        status="rejected",
                        reason="Sensitive information is not stored automatically.",
                        memory_type=extracted_memory.memory_type,
                        content=memory_content,
                        importance=extracted_memory.importance,
                        confidence=extracted_memory.confidence,
                    )
                )
                continue

            policy_decision = self.policy.evaluate(
                memory=extracted_memory,
            )

            if not policy_decision.allowed:
                item_results.append(
                    MemoryItemResult(
                        status="rejected",
                        reason=policy_decision.reason,
                        memory_type=extracted_memory.memory_type,
                        content=memory_content,
                        importance=extracted_memory.importance,
                        confidence=extracted_memory.confidence,
                    )
                )
                continue

            memory_type = policy_decision.memory_type

            if memory_type is None or memory_content is None:
                item_results.append(
                    MemoryItemResult(
                        status="rejected",
                        reason="Memory type or content is unavailable.",
                        confidence=extracted_memory.confidence,
                    )
                )
                continue

            # Step 3B: Find similar existing memories.
            duplicate_result = self.duplicate_detector.check_duplicate(
                user_id=user_id,
                memory_type=memory_type,
                content=memory_content,
            )

            # Step 3C: No candidate means CREATE.
            if not duplicate_result.candidates:
                memory = self.memory_service.create_memory(
                    user_id=user_id,
                    memory_type=memory_type,
                    content=memory_content,
                    importance=extracted_memory.importance,
                    confidence=extracted_memory.confidence,
                    source_conversation_id=source_conversation_id,
                )

                item_results.append(
                    MemoryItemResult(
                        status="stored",
                        reason="New memory successfully stored.",
                        memory_id=memory.id,
                        memory_type=memory_type,
                        content=memory.content,
                        importance=memory.importance,
                        confidence=memory.confidence,
                    )
                )
                continue

            # Step 3D: Compare against the best matching candidate.
            candidate = max(
                duplicate_result.candidates,
                key=lambda item: item.similarity,
            )

            # Exact normalized duplicate: no LLM call required.
            normalized_new = self.duplicate_detector.normalize_content(
                memory_content
            )

            normalized_existing = self.duplicate_detector.normalize_content(
                candidate.content
            )

            if normalized_new == normalized_existing:
                item_results.append(
                    MemoryItemResult(
                        status="duplicate",
                        reason="Exact duplicate memory detected.",
                        memory_id=candidate.memory_id,
                        memory_type=memory_type,
                        content=candidate.content,
                        importance=extracted_memory.importance,
                        confidence=extracted_memory.confidence,
                    )
                )
                continue

            # Step 3E: Resolve semantic similarity or conflict.
            decision = self.conflict_resolver.resolve(
                new_memory=memory_content,
                existing_memory=candidate.content,
            )

            if decision.action == "ignore":
                item_results.append(
                    MemoryItemResult(
                        status="duplicate",
                        reason=decision.reason,
                        memory_id=candidate.memory_id,
                        memory_type=memory_type,
                        content=candidate.content,
                        importance=extracted_memory.importance,
                        confidence=decision.confidence,
                    )
                )
                continue

            if decision.action == "review":

                review = self._create_pending_review(
                    user_id=user_id,
                    existing_memory_id=candidate.memory_id,
                    existing_content=candidate.content,
                    proposed_content=memory_content,
                    reason=decision.reason,
                    confidence=decision.confidence,
                )

                item_results.append(
                    MemoryItemResult(
                        status="review_required",
                        reason=(
                            f"{decision.reason} "
                            f"Pending review ID: {review.id}"
                        ),
                        memory_id=candidate.memory_id,
                        memory_type=memory_type,
                        content=memory_content,
                        importance=extracted_memory.importance,
                        confidence=decision.confidence,
                    )
                )
                continue

            if decision.action == "update":
                updated_content = decision.updated_content

                if not updated_content:

                    review = self._create_pending_review(
                        user_id=user_id,
                        existing_memory_id=candidate.memory_id,
                        existing_content=candidate.content,
                        proposed_content=memory_content,
                        reason="Updated content is unavailable. Manual review is required.",
                        confidence=decision.confidence,
                    )

                    item_results.append(
                        MemoryItemResult(
                            status="review_required",
                            reason=(
                                f"Updated content is unavailable. "
                                f"Pending review ID: {review.id}"
                            ),
                            memory_id=candidate.memory_id,
                            memory_type=memory_type,
                            content=memory_content,
                            importance=extracted_memory.importance,
                            confidence=decision.confidence,
                        )
                    )
                    continue

                updated_memory = self.memory_service.update_memory(
                    user_id=user_id,
                    memory_id=candidate.memory_id,
                    content=updated_content,
                    importance=extracted_memory.importance,
                    confidence=decision.confidence,
                )

                if updated_memory is None:
                    item_results.append(
                        MemoryItemResult(
                            status="review_required",
                            reason="Existing memory could not be updated.",
                            memory_id=candidate.memory_id,
                            memory_type=memory_type,
                            confidence=decision.confidence,
                        )
                    )
                    continue

                item_results.append(
                    MemoryItemResult(
                        status="updated",
                        reason=decision.reason,
                        memory_id=updated_memory.id,
                        memory_type=updated_memory.memory_type,
                        content=updated_memory.content,
                        importance=updated_memory.importance,
                        confidence=updated_memory.confidence,
                    )
                )
                continue

            # Step 3F: CREATE when the resolver determines
            # that both facts can coexist.
            if decision.action == "create":
                memory = self.memory_service.create_memory(
                    user_id=user_id,
                    memory_type=memory_type,
                    content=memory_content,
                    importance=extracted_memory.importance,
                    confidence=extracted_memory.confidence,
                    source_conversation_id=source_conversation_id,
                )

                item_results.append(
                    MemoryItemResult(
                        status="stored",
                        reason=decision.reason,
                        memory_id=memory.id,
                        memory_type=memory_type,
                        content=memory.content,
                        importance=memory.importance,
                        confidence=memory.confidence,
                    )
                )

        # Step 4: Aggregate results.
        statuses = [item.status for item in item_results]

        if "stored" in statuses:
            overall_status = "stored"
            overall_reason = "One or more new memories were stored."

        elif "updated" in statuses:
            overall_status = "updated"
            overall_reason = "One or more existing memories were updated."

        elif "review_required" in statuses:
            overall_status = "review_required"
            overall_reason = "One or more memories require review."

        elif statuses and all(
            status == "duplicate" for status in statuses
        ):
            overall_status = "duplicate"
            overall_reason = "All extracted memories already exist."

        else:
            overall_status = "rejected"
            overall_reason = "No memories were stored or updated."

        successful_items = [
            item for item in item_results
            if item.status in {"stored", "updated"}
        ]

        primary_item = (
            successful_items[0]
            if successful_items
            else (item_results[0] if item_results else None)
        )

        return MemoryPipelineResult(
            status=overall_status,
            reason=overall_reason,
            memory_id=primary_item.memory_id if primary_item else None,
            memory_type=primary_item.memory_type if primary_item else None,
            confidence=primary_item.confidence if primary_item else None,
            items=item_results,
        )
