
import logging
from typing import Literal

from pydantic import BaseModel, Field, ConfigDict

from app.llm.router import llm_router
from app.llm.schemas import LLMRequest, LLMMessage


logger = logging.getLogger(__name__)


MemoryType = Literal[
    "long_term",
    "episodic",
    "semantic",
    "procedural",
]


class ExtractedMemory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    should_store: bool

    memory_type: MemoryType | None = None

    content: str | None = Field(
        default=None,
        max_length=1000,
    )

    importance: int = Field(
        default=5,
        ge=1,
        le=10,
    )

    confidence: float = Field(
        ge=0.0,
        le=1.0,
    )

    reason: str

    contains_sensitive_information: bool = False


class MemoryClassification(BaseModel):
    """
    Structured collection of memories extracted from one user message.
    """

    memories: list[ExtractedMemory] = Field(
        default_factory=list,
        max_length=10,
    )


class MemoryClassifier:
    """
    Extracts meaningful user-specific memories from natural conversation.

    Uses the shared LLM router and configured provider fallback.
    Does not directly save anything to the database.
    """

    SYSTEM_PROMPT = """
You are an intelligent Personal AI Assistant Memory Extraction Engine.

Your responsibility is to identify meaningful information about
the user from their natural conversation.

The user should NOT need to explicitly say:
"Remember this", "Save this", or "Store this".

Understand the meaning of normal conversation and extract
important information automatically.

AVAILABLE MEMORY TYPES:

1. long_term
Stable personal preferences, interests, goals and characteristics.

Examples:
- I prefer explanations in Bengali.
- I prefer Python over JavaScript.
- I enjoy travelling with my family.

2. episodic
Specific events, experiences, activities and decisions.

Examples:
- I attended an AI interview yesterday.
- I completed my project last week.
- I visited Kolkata last month.

3. semantic
User-specific facts, professional information, ongoing projects,
technical stacks and meaningful relationships.

Examples:
- I work as a frontend developer.
- My project uses FastAPI and PostgreSQL.
- My backend uses LangGraph.
- I have five years of development experience.

4. procedural
Recurring workflows, habits and instructions.

Examples:
- Always explain technical lessons step by step.
- I prefer using PowerShell for Python development.
- I review my emails every morning.

EXTRACTION RULES:

- Extract multiple independent memories when appropriate.
- Extract only information genuinely supported by the user's message.
- Do not invent facts or assumptions.
- Do not store ordinary questions, greetings, jokes or small talk.
- Do not store general knowledge merely because the user asks about it.
- Do not interpret hypothetical examples as personal facts.
- Do not interpret quoted or pasted text as the user's own facts
  unless the user clearly adopts those facts.
- Do not store temporary information as permanent preferences.
- Extract concise, standalone, meaningful memory statements.
- Preserve important context and temporal qualifiers.
- Never add facts that the user did not provide.

IMPORTANCE:

1-3: Minor or low-value information.
4-6: Useful contextual information.
7-8: Important personal facts, professional information and goals.
9-10: Highly important stable preferences or ongoing objectives.

SENSITIVE INFORMATION:

- Identify sensitive personal information.
- Set contains_sensitive_information to true when applicable.
- Sensitive information must not be automatically stored.
- Never attempt to extract passwords, authentication tokens,
  API keys, financial credentials or security secrets.

WHEN NOTHING IS WORTH REMEMBERING:

Return an empty memories array.

OUTPUT:

Return valid JSON only.
Do not use Markdown code fences.

Required structure:

{
  "memories": [
    {
      "should_store": true,
      "memory_type": "long_term",
      "content": "User prefers explanations in Bengali.",
      "importance": 9,
      "confidence": 0.98,
      "reason": "A stable communication preference.",
      "contains_sensitive_information": false
    }
  ]
}

If no meaningful memory exists:

{
  "memories": []
}
"""

    def classify(self, text: str) -> MemoryClassification:
        """
        Extract and classify multiple memories from a user message.
        """

        if not text or not text.strip():
            raise ValueError("Text cannot be empty.")

        request = LLMRequest(
            messages=[
                LLMMessage(
                    role="system",
                    content=self.SYSTEM_PROMPT,
                ),
                LLMMessage(
                    role="user",
                    content=text.strip(),
                ),
            ],
            temperature=0.0,
            max_tokens=1200,
        )

        response = llm_router.generate(request)

        try:
            result = MemoryClassification.model_validate_json(
                response.content.strip()
            )

        except Exception as error:
            logger.error(
                "Memory extraction validation failed | "
                "provider=%s | model=%s",
                response.provider,
                response.model,
            )

            raise ValueError(
                "AI returned an invalid memory extraction response."
            ) from error

        # Apply deterministic validation after LLM extraction.
        safe_memories = []

        for memory in result.memories:

            if memory.contains_sensitive_information:
                logger.info(
                    "Sensitive memory candidate rejected."
                )
                continue

            if not memory.should_store:
                continue

            if not memory.memory_type:
                logger.warning(
                    "Memory candidate rejected: missing memory type."
                )
                continue

            if not memory.content or not memory.content.strip():
                logger.warning(
                    "Memory candidate rejected: empty content."
                )
                continue

            memory.content = memory.content.strip()

            safe_memories.append(memory)

        result.memories = safe_memories

        logger.info(
            "Memory extraction completed | candidates=%s | "
            "accepted=%s | provider=%s",
            len(result.memories),
            len(safe_memories),
            response.provider,
        )

        return result
