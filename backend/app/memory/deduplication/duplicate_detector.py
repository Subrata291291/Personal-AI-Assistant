import uuid
from typing import Literal

from pydantic import BaseModel, Field

from sqlalchemy.orm import Session

from app.services.memory_service import MemoryService


STRONG_DUPLICATE_THRESHOLD = 0.95
POSSIBLE_DUPLICATE_THRESHOLD = 0.80


class DuplicateCandidate(BaseModel):
    memory_id: uuid.UUID
    memory_type: str
    content: str
    similarity: float


class DuplicateCheckResult(BaseModel):
    status: Literal[
        "strong_duplicate",
        "possible_duplicate",
        "new_memory",
    ]

    is_duplicate: bool
    reason: str

    candidates: list[DuplicateCandidate] = Field(
        default_factory=list
    )


class DuplicateDetector:

    def __init__(self, session: Session):
        self.memory_service = MemoryService(session)

    @staticmethod
    def normalize_content(content: str) -> str:
        return " ".join(content.casefold().split())

    def check_duplicate(
        self,
        user_id: uuid.UUID,
        memory_type: str,
        content: str,
    ) -> DuplicateCheckResult:

        if not content or not content.strip():
            raise ValueError(
                "Memory content cannot be empty."
            )

        memory_type = memory_type.strip().lower()

        if memory_type not in {
            "long_term",
            "episodic",
            "semantic",
            "procedural",
        }:
            raise ValueError("Invalid memory type.")

        normalized_content = self.normalize_content(content)

        results = self.memory_service.search_similar_memories(
            user_id=user_id,
            query=content,
            limit=20,
            min_similarity=POSSIBLE_DUPLICATE_THRESHOLD,
        )

        candidates = []

        for memory, similarity in results:

            # Compare only memories from the same category.
            if memory.memory_type != memory_type:
                continue

            existing_content = self.normalize_content(
                memory.content
            )

            # Exact duplicate after normalization.
            if existing_content == normalized_content:
                return DuplicateCheckResult(
                    status="strong_duplicate",
                    is_duplicate=True,
                    reason="Exact duplicate memory detected.",
                    candidates=[
                        DuplicateCandidate(
                            memory_id=memory.id,
                            memory_type=memory.memory_type,
                            content=memory.content,
                            similarity=similarity,
                        )
                    ],
                )

            candidates.append(
                DuplicateCandidate(
                    memory_id=memory.id,
                    memory_type=memory.memory_type,
                    content=memory.content,
                    similarity=similarity,
                )
            )

        if not candidates:
            return DuplicateCheckResult(
                status="new_memory",
                is_duplicate=False,
                reason="No similar memory found in this category.",
            )

        candidates.sort(
            key=lambda item: item.similarity,
            reverse=True,
        )

        best_candidate = candidates[0]

        if best_candidate.similarity >= STRONG_DUPLICATE_THRESHOLD:
            return DuplicateCheckResult(
                status="strong_duplicate",
                is_duplicate=True,
                reason="Strong semantic duplicate candidate detected.",
                candidates=candidates,
            )

        return DuplicateCheckResult(
            status="possible_duplicate",
            is_duplicate=False,
            reason="Possible semantic duplicate requires review.",
            candidates=candidates,
        )