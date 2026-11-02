
import uuid
from datetime import datetime
from typing import Any
from sqlalchemy import select, update, delete, desc, func, or_
from sqlalchemy.orm import Session

from app.db.models.memory import Memory
from sqlalchemy import select, func
from app.db.models.conversation import Conversation

class _Unset:
    """Distinguishes an omitted value from an explicit None."""
    pass


UNSET = _Unset()

class MemoryRepository:
    """
    Database repository for persistent user memories.

    Every operation explicitly scopes records by user_id.
    This is mandatory because the backend DB role bypasses RLS.
    """

    def __init__(self, session: Session):
        self.session = session

    
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

        # Verify that the source conversation belongs to this user.
        if source_conversation_id is not None:
            if not self.verify_source_conversation(
                user_id,
                source_conversation_id,
            ):
                raise ValueError(
                    "Source conversation does not belong to this user."
                )

        memory = Memory(
            user_id=user_id,
            memory_type=memory_type,
            content=content,
            embedding=embedding,
            importance=importance,
            confidence=confidence,
            metadata_=metadata,
            source_conversation_id=source_conversation_id,
            expires_at=expires_at,
        )

        self.session.add(memory)
        self.session.flush()

        return memory

    def get_memory(
        self,
        user_id: uuid.UUID,
        memory_id: uuid.UUID,
    ) -> Memory | None:

        statement = select(Memory).where(
            Memory.id == memory_id,
            Memory.user_id == user_id,
        )

        return self.session.scalar(statement)

    def list_memories(
        self,
        user_id: uuid.UUID,
        memory_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Memory]:

        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")

        if offset < 0:
            raise ValueError("offset cannot be negative")

        statement = select(Memory).where(
            Memory.user_id == user_id,
        )

        if memory_type is not None:
            statement = statement.where(
                Memory.memory_type == memory_type,
            )

        statement = (
            statement
            .order_by(desc(Memory.created_at))
            .limit(limit)
            .offset(offset)
        )

        return list(self.session.scalars(statement).all())
  
    def update_memory(
        self,
        user_id: uuid.UUID,
        memory_id: uuid.UUID,
        content: str | None = None,
        embedding: list[float] | None | _Unset = UNSET,
        importance: int | None = None,
        confidence: float | None = None,
        metadata: dict[str, Any] | None | _Unset = UNSET,
        expires_at: datetime | None | _Unset = UNSET,
    ) -> Memory | None:

        values: dict[str, Any] = {}

        if content is not None:
            values["content"] = content

        if not isinstance(embedding, _Unset):
            values["embedding"] = embedding

        if importance is not None:
            values["importance"] = importance

        if confidence is not None:
            values["confidence"] = confidence

        if not isinstance(metadata, _Unset):
            values["metadata_"] = metadata

        if not isinstance(expires_at, _Unset):
            values["expires_at"] = expires_at

        if not values:
            return self.get_memory(user_id, memory_id)

        values["updated_at"] = datetime.now().astimezone()

        statement = (
            update(Memory)
            .where(
                Memory.id == memory_id,
                Memory.user_id == user_id,
            )
            .values(**values)
            .returning(Memory)
        )

        return self.session.scalar(statement)

    def delete_memory(
        self,
        user_id: uuid.UUID,
        memory_id: uuid.UUID,
    ) -> bool:

        statement = (
            delete(Memory)
            .where(
                Memory.id == memory_id,
                Memory.user_id == user_id,
            )
            .returning(Memory.id)
        )

        deleted_id = self.session.scalar(statement)

        return deleted_id is not None

    def verify_source_conversation(
        self,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
    ) -> bool:

        statement = select(Conversation.id).where(
            Conversation.id == conversation_id,
            Conversation.user_id == user_id,
        )

        return self.session.scalar(statement) is not None


    def search_similar_memories(
        self,
        user_id: uuid.UUID,
        embedding: list[float],
        limit: int = 5,
    ) -> list[tuple[Memory, float]]:

        if len(embedding) != 768:
            raise ValueError(
                "Embedding must contain exactly 768 dimensions."
            )

        if not 1 <= limit <= 20:
            raise ValueError("limit must be between 1 and 20.")

        distance = Memory.embedding.cosine_distance(embedding)

        similarity = (1.0 - distance).label("similarity")

        statement = (
            select(Memory, similarity)
            .where(
                Memory.user_id == user_id,
                Memory.embedding.is_not(None),
                or_(
                    Memory.expires_at.is_(None),
                    Memory.expires_at > func.now(),
                ),
            )
            .order_by(distance)
            .limit(limit)
        )

        rows = self.session.execute(statement).all()

        return [
            (memory, float(score))
            for memory, score in rows
        ]