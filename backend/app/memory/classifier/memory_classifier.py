import json
import logging

from typing import Literal

from pydantic import BaseModel, Field

from app.llm.router import llm_router
from app.llm.schemas import LLMRequest, LLMMessage


logger = logging.getLogger(__name__)


MemoryType = Literal[
    "long_term",
    "episodic",
    "semantic",
    "procedural",
]


class MemoryClassification(BaseModel):
    """
    Structured output returned by the AI memory classifier.
    """

    should_store: bool

    memory_type: MemoryType | None = None

    confidence: float = Field(
        ge=0.0,
        le=1.0,
    )

    reason: str

    contains_sensitive_information: bool = False


class MemoryClassifier:
    """
    Classifies user information into persistent memory categories.

    Uses the shared LLM router and its configured provider fallback.
    Does not directly save anything to the database.
    """

    SYSTEM_PROMPT = """
You are an AI Memory Classification Engine.

Your task is to decide whether a user's information
should be stored as persistent memory.

Available memory categories:

1. long_term
Stable personal facts, preferences, goals and interests.

Examples:
- I prefer explanations in Bengali.
- I work as a frontend developer.
- I prefer travelling with my family.

2. episodic
Specific events, experiences, activities and decisions.

Examples:
- Yesterday I attended an AI interview.
- I visited Kolkata last week.
- I completed a project yesterday.

3. semantic
Facts, knowledge and meaningful relationships.

Examples:
- Kolkata is the capital of West Bengal.
- Python is a programming language.
- PostgreSQL is a relational database.

4. procedural
Recurring workflows, habits and instructions
about how tasks should be performed.

Examples:
- Always explain technical lessons step by step.
- I activate my virtual environment before installing packages.
- I review my emails every morning.

Rules:

- Do not store greetings or ordinary conversation.
- Do not store temporary questions as permanent memories.
- Do not invent personal facts.
- Do not confuse a question with a user preference.
- If information is not suitable for persistent memory,
  set should_store to false and memory_type to null.
- If information contains sensitive personal information,
  set contains_sensitive_information to true.
- Sensitive information must not be automatically stored.
- Select exactly one category when should_store is true.
- Return valid JSON only.
- Do not use Markdown code fences.

Required JSON structure:

{
    "should_store": true,
    "memory_type": "long_term",
    "confidence": 0.95,
    "reason": "A stable user preference.",
    "contains_sensitive_information": false
}

MEMORY CLASSIFICATION RULES:

1. LONG_TERM:
Stable personal preferences, interests, goals, and user characteristics.
Example: "I prefer Bengali explanations."

2. EPISODIC:
Specific events, experiences, activities, or decisions associated
with a particular time or occasion.
Example: "Yesterday, I attended an AI interview."

3. SEMANTIC:
User-specific factual knowledge, current project details,
technical stack, professional facts, and relationships.
These facts do not need to describe a specific event.
Example: "My current project uses FastAPI and PostgreSQL."
Example: "My backend uses LangGraph."
Example: "My application uses Supabase for authentication."

4. PROCEDURAL:
Recurring habits, routines, and methods describing how the user
performs tasks.
Example: "I review my emails every morning."

IMPORTANT DISTINCTION:

- A fact about an ongoing project belongs to SEMANTIC.
- An event describing something that happened at a particular
  time belongs to EPISODIC.
- Do not classify every project-related statement as EPISODIC.
- Prefer SEMANTIC for stable or ongoing factual information.
- Classify based on the meaning of the statement, not merely
  the presence of words such as "project", "yesterday", or "current".
"""

    def classify(self, text: str) -> MemoryClassification:
        """
        Classify a text into a persistent memory category.
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
            max_tokens=300,
        )

        response = llm_router.generate(request)

        try:
            result = MemoryClassification.model_validate_json(
                response.content.strip()
            )

        except Exception as error:
            logger.error(
                "Memory classification response validation failed | "
                "provider=%s | model=%s",
                response.provider,
                response.model,
            )

            raise ValueError(
                "AI returned an invalid memory classification."
            ) from error

        # Sensitive information must never be automatically stored.
        if result.contains_sensitive_information:
            result.should_store = False
            result.memory_type = None

        # A non-storable result cannot have a persistent category.
        if not result.should_store:
            result.memory_type = None

        if result.should_store and result.memory_type is None:
            raise ValueError(
                "A storable memory must have a memory type."
            )

        logger.info(
            "Memory classified | type=%s | should_store=%s | "
            "confidence=%.2f | provider=%s",
            result.memory_type,
            result.should_store,
            result.confidence,
            response.provider,
        )

        return result