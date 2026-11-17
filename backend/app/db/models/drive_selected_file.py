from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DriveSelectedFile(Base):
    """Minimal metadata for a file the user explicitly selected in Picker.

    Contract text is never stored here. Google Drive remains the source of
    truth; this table only records which files the app may read for the user.
    """

    __tablename__ = "pa_drive_selected_files"

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "provider_file_id",
            name="uq_pa_drive_selected_files_user_file",
        ),
        ForeignKeyConstraint(
            ["connection_id", "user_id"],
            [
                "pa_user_connections.id",
                "pa_user_connections.user_id",
            ],
            name="fk_pa_drive_selected_files_connection_user",
            ondelete="CASCADE",
        ),
        Index(
            "ix_pa_drive_selected_files_user_connection",
            "user_id",
            "connection_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )

    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
    )

    connection_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
    )

    provider_file_id: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
    )

    name: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
    )

    mime_type: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    modified_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    size_bytes: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
    )

    selected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
