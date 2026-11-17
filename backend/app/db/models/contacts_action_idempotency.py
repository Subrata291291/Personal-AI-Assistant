import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ContactsActionIdempotency(Base):
    __tablename__ = "pa_contacts_action_idempotency"

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "user_id",
            "connection_id",
            "action_type",
            "idempotency_key",
            name="uq_pa_contacts_action_idempotency_scope_key",
        ),
        UniqueConstraint(
            "proposal_id",
            name="uq_pa_contacts_action_idempotency_proposal",
        ),
        CheckConstraint(
            "action_type = 'contacts.create_contact'",
            name="ck_pa_contacts_action_idempotency_action_type",
        ),
        CheckConstraint(
            "char_length(payload_fingerprint) = 64",
            name="ck_pa_contacts_action_idempotency_fingerprint",
        ),
        CheckConstraint(
            "char_length(idempotency_key) BETWEEN 1 AND 128",
            name="ck_pa_contacts_action_idempotency_key_len",
        ),
        CheckConstraint(
            "normalization_version >= 1",
            name="ck_pa_contacts_action_idempotency_norm_ver",
        ),
        CheckConstraint(
            "proposal_storage = 'conversation_message'",
            name="ck_pa_contacts_action_idempotency_storage",
        ),
        CheckConstraint(
            "request_hash_alg = 'sha256'",
            name="ck_pa_contacts_action_idempotency_hash_alg",
        ),
        Index(
            "ix_pa_contacts_action_idempotency_user_created",
            "user_id",
            "created_at",
        ),
        Index(
            "ix_pa_contacts_action_idempotency_replay_until",
            "replay_until",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )

    # Server-derived from authenticated user identity (Phase 2C.1):
    # no first-class tenant/workspace entity exists, so tenant_id == user_id.
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("auth.users.id", ondelete="CASCADE"),
        nullable=False,
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

    action_type: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )

    idempotency_key: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
    )

    payload_fingerprint: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )

    normalization_version: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=False,
        default=1,
        server_default=text("1"),
    )

    # Canonical action-approval proposal/message id.
    # Not FK-constrained: proposals are stored as pa_conversation_messages rows
    # with synthetic conversation/message lifecycle; cascading/restricting here
    # could break cleanup or silently drop replay protection.
    proposal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
    )

    proposal_storage: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="conversation_message",
        server_default=text("'conversation_message'"),
    )

    request_hash_alg: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="sha256",
        server_default=text("'sha256'"),
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

    replay_until: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
