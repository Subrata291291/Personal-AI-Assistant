from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


MemoryAction = Literal["CREATE", "UPDATE", "IGNORE", "REVIEW"]
MemoryType = Literal["long_term", "episodic", "semantic", "procedural"]


class MemoryDecision(BaseModel):
    action: MemoryAction
    memory_type: MemoryType
    target_memory_id: UUID | None = None
    proposed_content: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    importance: int = Field(default=5, ge=1, le=10)
    reason: str


class MemoryDecisionEngine:
    """Builds validated decisions from existing memory pipeline evidence."""

    @staticmethod
    def create(
        *,
        memory_type: MemoryType,
        proposed_content: str,
        confidence: float,
        importance: int,
        reason: str,
        target_memory_id: UUID | None = None,
        action: MemoryAction = "CREATE",
    ) -> MemoryDecision:
        return MemoryDecision(
            action=action,
            memory_type=memory_type,
            target_memory_id=target_memory_id,
            proposed_content=proposed_content if action != "IGNORE" else None,
            confidence=confidence,
            importance=importance,
            reason=reason,
        )
