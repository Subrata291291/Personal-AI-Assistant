
from typing import Literal

from pydantic import BaseModel

from app.memory.classifier.memory_classifier import (
    ExtractedMemory,
)


ALLOWED_MEMORY_TYPES = {
    "long_term",
    "episodic",
    "semantic",
    "procedural",
}

MIN_CONFIDENCE = 0.80


class MemoryPolicyDecision(BaseModel):
    allowed: bool
    reason: str

    memory_type: Literal[
        "long_term",
        "episodic",
        "semantic",
        "procedural",
    ] | None = None


class MemoryPolicy:
    """
    Validates each extracted memory independently
    before it can be saved.
    """

    def __init__(
        self,
        min_confidence: float = MIN_CONFIDENCE,
    ):
        if not 0.0 <= min_confidence <= 1.0:
            raise ValueError(
                "Minimum confidence must be between 0 and 1."
            )

        self.min_confidence = min_confidence

    def evaluate(
        self,
        memory: ExtractedMemory,
    ) -> MemoryPolicyDecision:

        # Rule 1: Validate storage decision.
        if not memory.should_store:
            return MemoryPolicyDecision(
                allowed=False,
                reason="Classifier decided not to store.",
            )

        # Rule 2: Reject sensitive information.
        if memory.contains_sensitive_information:
            return MemoryPolicyDecision(
                allowed=False,
                reason="Sensitive information detected.",
            )

        # Rule 3: Validate extracted content.
        if not memory.content or not memory.content.strip():
            return MemoryPolicyDecision(
                allowed=False,
                reason="Empty memory content.",
            )

        # Rule 4: Validate confidence.
        if memory.confidence < self.min_confidence:
            return MemoryPolicyDecision(
                allowed=False,
                reason="Classification confidence is too low.",
            )

        # Rule 5: Validate memory category.
        if memory.memory_type not in ALLOWED_MEMORY_TYPES:
            return MemoryPolicyDecision(
                allowed=False,
                reason="Invalid memory category.",
            )

        # Rule 6: Validate importance.
        if not 1 <= memory.importance <= 10:
            return MemoryPolicyDecision(
                allowed=False,
                reason="Invalid memory importance.",
            )

        return MemoryPolicyDecision(
            allowed=True,
            reason="Memory passed policy validation.",
            memory_type=memory.memory_type,
        )
