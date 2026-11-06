
from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class OAuthState(Base):
    __tablename__ = "pa_oauth_states"

    __table_args__ = (
        CheckConstraint(
            "service IS NULL OR service IN ('gmail', 'calendar', 'calendar_write')",
            name="ck_pa_oauth_states_service",
        ),
        UniqueConstraint(
            "state_hash",
            name="uq_pa_oauth_states_state_hash",
        ),
        Index(
            "ix_pa_oauth_states_user_provider",
            "user_id",
            "provider",
        ),
        Index(
            "ix_pa_oauth_states_expires_at",
            "expires_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )

    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("auth.users.id", ondelete="CASCADE"),
        nullable=False,
    )

    provider: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )

    # Store SHA-256 hash of the random OAuth state,
    # never the original state value.
    state_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )

    # PKCE verifier is stored encrypted.
    code_verifier_encrypted: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    redirect_uri: Mapped[str] = mapped_column(
        String(2048),
        nullable=False,
    )

    service: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
    )

    target_connection_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("pa_user_connections.id", ondelete="SET NULL"),
        nullable=True,
    )

    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
