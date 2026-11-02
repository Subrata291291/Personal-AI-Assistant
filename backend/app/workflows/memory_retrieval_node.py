import logging
from uuid import UUID
from typing import Any

from app.db.session import SessionLocal
from app.services.memory_service import MemoryService
from app.workflows.chat_state import ChatState


logger = logging.getLogger(__name__)

MEMORY_RETRIEVAL_LIMIT = 5
MEMORY_SIMILARITY_THRESHOLD = 0.70


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

            results = memory_service.search_similar_memories(
                user_id=user_id,
                query=query,
                limit=MEMORY_RETRIEVAL_LIMIT,
                min_similarity=MEMORY_SIMILARITY_THRESHOLD,
            )

            retrieved_memories = []

            for memory, similarity in results:
                retrieved_memories.append({
                    "memory_id": str(memory.id),
                    "memory_type": memory.memory_type,
                    "content": memory.content,
                    "similarity": round(similarity, 4),
                })

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
                context_parts.append(
                    f"{index}. "
                    f"[{memory['memory_type']}] "
                    f"{memory['content']}"
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