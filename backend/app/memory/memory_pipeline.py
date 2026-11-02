import uuid
from typing import Literal

from pydantic import BaseModel

from sqlalchemy.orm import Session

from app.memory.classifier.memory_classifier import MemoryClassifier
from app.memory.classifier.memory_policy import MemoryPolicy
from app.memory.deduplication.duplicate_detector import DuplicateDetector
from app.services.memory_service import MemoryService


class MemoryPipelineResult(BaseModel):
    status: Literal[
        "stored",
        "rejected",
        "duplicate",
        "review_required",
        "disabled",
    ]

    reason: str
    memory_id: uuid.UUID | None = None
    memory_type: str | None = None
    confidence: float | None = None


class MemoryPipeline:

    def __init__(self, session: Session):

        self.classifier = MemoryClassifier()
        self.policy = MemoryPolicy()

        self.memory_service = MemoryService(session)
        self.duplicate_detector = DuplicateDetector(session)

    def process(
        self,
        user_id: uuid.UUID,
        content: str,
        memory_enabled: bool = False,
        source_conversation_id: uuid.UUID | None = None,
    ) -> MemoryPipelineResult:

        # Step 1: Check user memory preference.
        # Fail closed: memory is disabled unless explicitly enabled.
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

        # Step 2: Classify the content.
        classification = self.classifier.classify(content)

        # Step 3: Validate classification.
        policy_decision = self.policy.evaluate(
            content=content,
            classification=classification,
        )

        if not policy_decision.allowed:
            return MemoryPipelineResult(
                status="rejected",
                reason=policy_decision.reason,
                confidence=classification.confidence,
            )

        memory_type = policy_decision.memory_type

        if memory_type is None:
            return MemoryPipelineResult(
                status="rejected",
                reason="Memory type is unavailable.",
                confidence=classification.confidence,
            )

        # Step 4: Check duplicates.
        duplicate_result = self.duplicate_detector.check_duplicate(
            user_id=user_id,
            memory_type=memory_type,
            content=content,
        )

        if duplicate_result.status == "strong_duplicate":
            return MemoryPipelineResult(
                status="duplicate",
                reason=duplicate_result.reason,
                memory_type=memory_type,
                confidence=classification.confidence,
            )

        if duplicate_result.status == "possible_duplicate":
            return MemoryPipelineResult(
                status="review_required",
                reason=duplicate_result.reason,
                memory_type=memory_type,
                confidence=classification.confidence,
            )

        # Step 5: Store new memory.
        memory = self.memory_service.create_memory(
            user_id=user_id,
            memory_type=memory_type,
            content=content,
            confidence=classification.confidence,
            source_conversation_id=source_conversation_id,
        )

        return MemoryPipelineResult(
            status="stored",
            reason="Memory successfully stored.",
            memory_id=memory.id,
            memory_type=memory_type,
            confidence=classification.confidence,
        )