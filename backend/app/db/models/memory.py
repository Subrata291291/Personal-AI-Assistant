
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    SmallInteger,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from pgvector.sqlalchemy import Vector

from app.db.base import Base


class Memory(Base):
    """
    Stores persistent memories belonging to individual Supabase users.

    Supported memory types:
    - long_term
    - episodic
    - semantic
    - procedural
    """

    __tablename__ = "pa_memories"

    __table_args__ = (
        CheckConstraint(
            "memory_type IN "
            "('long_term', 'episodic', 'semantic', 'procedural')",
            name="ck_pa_memories_memory_type",
        ),
        CheckConstraint(
            "importance BETWEEN 1 AND 10",
            name="ck_pa_memories_importance",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_pa_memories_confidence",
        ),
        Index(
            "ix_pa_memories_user_type",
            "user_id",
            "memory_type",
        ),
        Index(
            "ix_pa_memories_user_created",
            "user_id",
            "created_at",
        ),
        Index(
            "ix_pa_memories_source_conversation",
            "source_conversation_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("auth.users.id", ondelete="CASCADE"),
        nullable=False,
    )

    memory_type: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    content: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(768),
        nullable=True,
    )
    importance: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=False,
        default=5,
        server_default="5",
    )

    confidence: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=1.0,
        server_default="1.0",
    )

    metadata_: Mapped[dict[str, Any] | None] = mapped_column(
        "metadata",
        JSONB,
        nullable=True,
    )

    source_conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("pa_conversations.id", ondelete="SET NULL"),
        nullable=True,
    )

    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
