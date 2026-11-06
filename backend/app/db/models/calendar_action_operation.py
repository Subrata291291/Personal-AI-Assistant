import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CalendarActionOperation(Base):
    __tablename__ = "pa_calendar_action_operations"

    __table_args__ = (
        CheckConstraint(
            "action_type IN ("
            "'calendar.create_event', 'calendar.update_event', "
            "'calendar.delete_event')",
            name="ck_pa_calendar_action_operations_action_type",
        ),
        CheckConstraint(
            "execution_state IN ("
            "'pending', 'approved', 'executing', 'completed', 'failed', "
            "'expired', 'rejected', 'cancelled', 'execution_uncertain')",
            name="ck_pa_calendar_action_operations_execution_state",
        ),
        CheckConstraint(
            "reconciliation_status IN ("
            "'not_required', 'pending', 'confirmed', 'not_executed', "
            "'not_found', 'conflict', 'manual_review')",
            name="ck_pa_calendar_action_operations_reconciliation_status",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_pa_calendar_action_operations_attempt_count",
        ),
        Index(
            "uq_pa_calendar_action_operations_user_idempotency",
            "user_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
        Index(
            "ix_pa_calendar_action_operations_state_updated",
            "execution_state",
            "updated_at",
        ),
    )

    proposal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
    )
    operation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        unique=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("auth.users.id", ondelete="CASCADE"),
        nullable=False,
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
    )
    idempotency_key: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
    )
    payload_fingerprint: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    action_type: Mapped[str] = mapped_column(Text, nullable=False)
    calendar_id: Mapped[str] = mapped_column(Text, nullable=False)
    event_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    execution_state: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        server_default="pending",
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default="0",
    )
    execution_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    execution_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    provider_result_classification: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    reconciliation_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        server_default="not_required",
    )
    reconciled_at: Mapped[datetime | None] = mapped_column(
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
