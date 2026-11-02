
from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    DateTime,
    ForeignKeyConstraint,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class OAuthCredential(Base):

    __tablename__ = "pa_oauth_credentials"

    __table_args__ = (
        ForeignKeyConstraint(
            ["connection_id", "user_id"],
            [
                "pa_user_connections.id",
                "pa_user_connections.user_id",
            ],
            ondelete="CASCADE",
            name="fk_pa_oauth_credentials_connection_user",
        ),
    )

    connection_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
    )

    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
    )

    access_token_encrypted: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    refresh_token_encrypted: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    token_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        server_default="Bearer",
    )

    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    scopes: Mapped[list[str]] = mapped_column(
        ARRAY(String(255)),
        nullable=False,
        server_default="{}",
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
