import logging
import re
from uuid import UUID
from typing import Any

from app.db.session import SessionLocal
from app.services.memory_service import MemoryService
from app.workflows.chat_state import ChatState


logger = logging.getLogger(__name__)

MEMORY_RETRIEVAL_LIMIT = 5
MEMORY_SIMILARITY_THRESHOLD = 0.70


def _explicit_memory_scope(query: str) -> str | None:
    lowered = query.casefold()
    if re.search(r"\ball\b.{0,40}\b(?:episodic\s+memor(?:y|ies)|memor(?:y|ies))\b|"
                 r"\bepisodic\s+memor(?:y|ies)\b.{0,40}\ball\b", lowered):
        return "episodic"
    if re.search(r"\b(?:memor(?:y|ies)|saved\s+records)\b", lowered):
        return "episodic" if re.search(r"\bepisodic\b", lowered) else "all"
    return None


def _relevance_rank(memory: Any, query: str) -> int:
    lowered_query = query.casefold()
    content = str(getattr(memory, "content", "")).casefold()
    terms = [
        term
        for term in re.findall(r"[a-z0-9]+", lowered_query)
        if term not in {
            "all", "stored", "memory", "memories", "episodic", "saved",
            "records", "retrieve", "only", "my", "list", "what", "have",
            "do", "not", "check", "google", "calendar",
        }
    ]
    return sum(1 for term in terms if term in content)


def memory_retrieval_node(
    state: ChatState,
) -> dict[str, Any]:
    """
    Retrieve relevant persistent memories for the current user.

    Memory retrieval is user-scoped.
    Retrieval failures do not stop the chat workflow.
    """

    user_id = UUID(state["user_id"])
    query = state["user_message"].strip()

    if not query:
        return {
            "retrieved_memories": [],
            "memory_context": "",
        }

    try:
        with SessionLocal() as session:

            memory_service = MemoryService(session)

            retrieved_memories = []
            memory_scope = _explicit_memory_scope(query)

            if memory_scope is not None:
                memory_type = "episodic" if memory_scope == "episodic" else None
                memories = memory_service.list_memories(
                    user_id=user_id,
                    memory_type=memory_type,
                    limit=100,
                    offset=0,
                )
                if re.search(r"\b(?:last|latest)\b", query.casefold()):
                    memories = sorted(
                        memories,
                        key=lambda item: (
                            _relevance_rank(item, query),
                            getattr(item, "created_at", None),
                        ),
                        reverse=True,
                    )
                elif re.search(r"\b(?:meetings?|gym|session)\b", query.casefold()):
                    memories = sorted(
                        memories,
                        key=lambda item: _relevance_rank(item, query),
                        reverse=True,
                    )

                for memory in memories:
                    record = {
                        "memory_id": str(memory.id),
                        "memory_type": memory.memory_type,
                        "content": memory.content,
                    }
                    created_at = getattr(memory, "created_at", None)
                    if created_at is not None:
                        record["created_at"] = created_at.isoformat()
                    metadata = getattr(memory, "metadata_", None)
                    if isinstance(metadata, dict) and metadata:
                        record["metadata"] = metadata
                    retrieved_memories.append(record)
            else:
                results = memory_service.search_similar_memories(
                    user_id=user_id,
                    query=query,
                    limit=MEMORY_RETRIEVAL_LIMIT,
                    min_similarity=MEMORY_SIMILARITY_THRESHOLD,
                )
                for memory, similarity in results:
                    record = {
                        "memory_id": str(memory.id),
                        "memory_type": memory.memory_type,
                        "content": memory.content,
                        "similarity": round(similarity, 4),
                    }
                    created_at = getattr(memory, "created_at", None)
                    if created_at is not None:
                        record["created_at"] = created_at.isoformat()
                    retrieved_memories.append(record)

            if not retrieved_memories:
                return {
                    "retrieved_memories": [],
                    "memory_context": "",
                }

            context_parts = []

            for index, memory in enumerate(
                retrieved_memories,
                start=1,
            ):
                timestamp = memory.get("created_at")
                metadata = memory.get("metadata")
                date_note = ""
                if isinstance(timestamp, str) and timestamp:
                    date_note = f" | stored_at={timestamp}"
                elif isinstance(metadata, dict):
                    stored_date = (
                        metadata.get("date")
                        or metadata.get("datetime")
                        or metadata.get("timestamp")
                        or metadata.get("occurred_at")
                    )
                    if isinstance(stored_date, str) and stored_date:
                        date_note = f" | recorded_date={stored_date}"
                context_parts.append(
                    f"{index}. "
                    f"[{memory['memory_type']}] "
                    f"{memory['content']}"
                    f"{date_note}"
                )

            memory_context = "\n".join(context_parts)

            logger.info(
                "Memory retrieval completed | count=%s",
                len(retrieved_memories),
            )

            return {
                "retrieved_memories": retrieved_memories,
                "memory_context": memory_context,
            }

    except Exception:
        logger.exception(
            "Memory retrieval failed. Continuing without memory context."
        )

        return {
            "retrieved_memories": [],
            "memory_context": "",
        }