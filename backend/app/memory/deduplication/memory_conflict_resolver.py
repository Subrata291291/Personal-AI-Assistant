
import logging
from typing import Literal

from pydantic import BaseModel, Field

from app.llm.router import llm_router
from app.llm.schemas import LLMRequest, LLMMessage


logger = logging.getLogger(__name__)


ConflictAction = Literal[
    "create",
    "ignore",
    "update",
    "review",
]


class MemoryConflictDecision(BaseModel):
    action: ConflictAction

    confidence: float = Field(
        ge=0.0,
        le=1.0,
    )

    reason: str

    updated_content: str | None = Field(
        default=None,
        max_length=1000,
    )


class MemoryConflictResolver:
    """
    Determines how a newly extracted memory relates
    to an existing memory.
    
    This class only makes a decision.
    It never modifies the database directly.
    """

    SYSTEM_PROMPT = """
You are a Memory Conflict Resolution Engine.

You receive:
1. A newly extracted memory.
2. An existing memory from the same user.

Determine the correct action.

AVAILABLE ACTIONS:

CREATE:
The new information is independent of the existing memory.
Both can remain valid.

IGNORE:
The new information is already represented by the existing memory.
No update is necessary.

UPDATE:
The user explicitly corrected, replaced, or updated the
previous information.

REVIEW:
There is an ambiguous conflict. Human confirmation is required.

IMPORTANT RULES:

- Never invent information.
- Do not treat additional interests as replacement preferences.
- Do not delete valid historical events.
- Do not update a memory merely because two memories are similar.
- Explicit corrections such as "I now prefer JavaScript instead
  of Python" can justify UPDATE.
- "I am also learning JavaScript" does not replace Python.
- If the relationship is uncertain, choose REVIEW.
- Preserve important context when producing updated_content.
- Never output sensitive information unnecessarily.

For UPDATE, provide a complete replacement memory statement.
For CREATE and IGNORE, updated_content must be null.
For REVIEW, updated_content must be null.

Return valid JSON only.

Example UPDATE:
{
  "action": "update",
  "confidence": 0.98,
  "reason": "The user explicitly replaced the previous preference.",
  "updated_content": "User prefers JavaScript instead of Python."
}

Example CREATE:
{
  "action": "create",
  "confidence": 0.95,
  "reason": "The user is exploring an additional programming language.",
  "updated_content": null
}

Example IGNORE:
{
  "action": "ignore",
  "confidence": 0.99,
  "reason": "The same preference is already stored.",
  "updated_content": null
}

Example REVIEW:
{
  "action": "review",
  "confidence": 0.90,
  "reason": "The relationship between the two facts is ambiguous.",
  "updated_content": null
}
"""

    MIN_CONFIDENCE = 0.85

    def resolve(
        self,
        new_memory: str,
        existing_memory: str,
    ) -> MemoryConflictDecision:

        if not new_memory or not new_memory.strip():
            raise ValueError("New memory cannot be empty.")

        if not existing_memory or not existing_memory.strip():
            raise ValueError("Existing memory cannot be empty.")

        request = LLMRequest(
            messages=[
                LLMMessage(
                    role="system",
                    content=self.SYSTEM_PROMPT,
                ),
                LLMMessage(
                    role="user",
                    content=(
                        "New memory:\n"
                        f"{new_memory.strip()}\n\n"
                        "Existing memory:\n"
                        f"{existing_memory.strip()}"
                    ),
                ),
            ],
            temperature=0.0,
            max_tokens=400,
        )

        response = llm_router.generate(request)

        try:
            decision = MemoryConflictDecision.model_validate_json(
                response.content.strip()
            )

        except Exception as error:
            logger.exception(
                "Memory conflict decision validation failed."
            )

            raise ValueError(
                "Invalid conflict resolution response."
            ) from error

        if decision.confidence < self.MIN_CONFIDENCE:
            return MemoryConflictDecision(
                action="review",
                confidence=decision.confidence,
                reason="Conflict resolution confidence is too low.",
            )

        if decision.action == "update":
            if (
                not decision.updated_content
                or not decision.updated_content.strip()
            ):
                return MemoryConflictDecision(
                    action="review",
                    confidence=decision.confidence,
                    reason="Updated content was not provided.",
                )

            decision.updated_content = (
                decision.updated_content.strip()
            )

        logger.info(
            "Memory conflict resolved | action=%s | confidence=%.2f",
            decision.action,
            decision.confidence,
        )

        return decision
