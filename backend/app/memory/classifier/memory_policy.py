from typing import Literal

from pydantic import BaseModel

from app.memory.classifier.memory_classifier import (
    MemoryClassification,
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
        content: str,
        classification: MemoryClassification,
    ) -> MemoryPolicyDecision:

        # Rule 1: Validate content.
        if not content or not content.strip():
            return MemoryPolicyDecision(
                allowed=False,
                reason="Empty memory content.",
            )

        # Rule 2: Respect the classifier's decision.
        if not classification.should_store:
            return MemoryPolicyDecision(
                allowed=False,
                reason="Classifier decided not to store.",
            )

        # Rule 3: Reject sensitive information.
        if classification.contains_sensitive_information:
            return MemoryPolicyDecision(
                allowed=False,
                reason="Sensitive information detected.",
            )

        # Rule 4: Check confidence.
        if classification.confidence < self.min_confidence:
            return MemoryPolicyDecision(
                allowed=False,
                reason="Classification confidence is too low.",
            )

        # Rule 5: Validate memory category.
        if classification.memory_type not in ALLOWED_MEMORY_TYPES:
            return MemoryPolicyDecision(
                allowed=False,
                reason="Invalid memory category.",
            )

        return MemoryPolicyDecision(
            allowed=True,
            reason="Memory passed policy validation.",
            memory_type=classification.memory_type,
        )