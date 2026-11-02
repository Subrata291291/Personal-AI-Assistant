
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session
from app.memory.embedding_service import EmbeddingService
from app.repositories.memory_repository import MemoryRepository, UNSET
from app.db.models.memory import Memory


ALLOWED_MEMORY_TYPES = {
    "long_term",
    "episodic",
    "semantic",
    "procedural",
}


class MemoryService:
    """
    Application service for persistent memory operations.

    Responsibilities:
    - Input validation
    - User-scoped memory operations
    - Transaction commit and rollback
    """

    def __init__(self, session: Session):
        self.session = session
        self.repository = MemoryRepository(session)

    def create_memory(
        self,
        user_id: uuid.UUID,
        memory_type: str,
        content: str,
        embedding: list[float] | None = None,
        importance: int = 5,
        confidence: float = 1.0,
        metadata: dict[str, Any] | None = None,
        source_conversation_id: uuid.UUID | None = None,
        expires_at: datetime | None = None,
    ) -> Memory:

        memory_type = memory_type.strip().lower()
        content = content.strip()

        if memory_type not in ALLOWED_MEMORY_TYPES:
            raise ValueError("Invalid memory type.")

        if not content:
            raise ValueError("Memory content cannot be empty.")

        if not 1 <= importance <= 10:
            raise ValueError("Importance must be between 1 and 10.")

        if not 0.0 <= confidence <= 1.0:
            raise ValueError("Confidence must be between 0 and 1.")

        if embedding is not None:
            if len(embedding) != 768:
                raise ValueError(
                    "Embedding must contain exactly 768 dimensions."
                )

        try:

            if embedding is None:
                embedding = EmbeddingService().embed_text(content)
                
            memory = self.repository.create_memory(
                user_id=user_id,
                memory_type=memory_type,
                content=content,
                embedding=embedding,
                importance=importance,
                confidence=confidence,
                metadata=metadata,
                source_conversation_id=source_conversation_id,
                expires_at=expires_at,
            )

            self.session.commit()
            self.session.refresh(memory)

            return memory

        except Exception:
            self.session.rollback()
            raise

    def get_memory(
        self,
        user_id: uuid.UUID,
        memory_id: uuid.UUID,
    ) -> Memory | None:

        return self.repository.get_memory(
            user_id=user_id,
            memory_id=memory_id,
        )

    def list_memories(
        self,
        user_id: uuid.UUID,
        memory_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Memory]:

        if memory_type is not None:
            memory_type = memory_type.strip().lower()

            if memory_type not in ALLOWED_MEMORY_TYPES:
                raise ValueError("Invalid memory type.")

        return self.repository.list_memories(
            user_id=user_id,
            memory_type=memory_type,
            limit=limit,
            offset=offset,
        )

    def update_memory(
        self,
        user_id: uuid.UUID,
        memory_id: uuid.UUID,
        content: str | None = None,
        embedding: Any = UNSET,
        importance: int | None = None,
        confidence: float | None = None,
        metadata: Any = UNSET,
        expires_at: Any = UNSET,
    ) -> Memory | None:

        if content is not None and not content.strip():
            raise ValueError("Memory content cannot be empty.")

        if importance is not None and not 1 <= importance <= 10:
            raise ValueError("Importance must be between 1 and 10.")

        if confidence is not None and not 0.0 <= confidence <= 1.0:
            raise ValueError("Confidence must be between 0 and 1.")

        if embedding is not UNSET and embedding is not None:
            if len(embedding) != 768:
                raise ValueError(
                    "Embedding must contain exactly 768 dimensions."
                )

        try:
            memory = self.repository.update_memory(
                user_id=user_id,
                memory_id=memory_id,
                content=content.strip() if content is not None else None,
                embedding=embedding,
                importance=importance,
                confidence=confidence,
                metadata=metadata,
                expires_at=expires_at,
            )

            if memory is None:
                self.session.rollback()
                return None

            self.session.commit()
            self.session.refresh(memory)

            return memory

        except Exception:
            self.session.rollback()
            raise

    def delete_memory(
        self,
        user_id: uuid.UUID,
        memory_id: uuid.UUID,
    ) -> bool:

        try:
            deleted = self.repository.delete_memory(
                user_id=user_id,
                memory_id=memory_id,
            )

            self.session.commit()

            return deleted

        except Exception:
            self.session.rollback()
            raise

    def search_similar_memories(
        self,
        user_id: uuid.UUID,
        query: str,
        limit: int = 5,
        min_similarity: float = 0.70,
    ) -> list[tuple[Memory, float]]:

        if not query or not query.strip():
            raise ValueError("Search query cannot be empty.")

        if not 1 <= limit <= 20:
            raise ValueError("limit must be between 1 and 20.")

        if not -1.0 <= min_similarity <= 1.0:
            raise ValueError(
                "min_similarity must be between -1 and 1."
            )

        embedding_service = EmbeddingService()

        query_embedding = embedding_service.embed_text(
            query.strip()
        )

        results = self.repository.search_similar_memories(
            user_id=user_id,
            embedding=query_embedding,
            limit=limit,
        )

        relevant_results = [
            (memory, similarity)
            for memory, similarity in results
            if similarity >= min_similarity
        ]

        return relevant_results