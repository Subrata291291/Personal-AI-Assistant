"""create Drive selected files allowlist and allow drive_file consent

Revision ID: 7c3e9b1a5d42
Revises: 1a7f4c9d2b6e
Create Date: 2026-11-16 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "7c3e9b1a5d42"
down_revision: Union[str, Sequence[str], None] = "1a7f4c9d2b6e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


PREVIOUS_SERVICE_CHECK = (
    "service IS NULL OR service IN "
    "('gmail', 'calendar', 'calendar_write', 'calendar_list_read', "
    "'contacts_read', 'contacts_write', 'drive_read', 'drive_write')"
)

EXPANDED_SERVICE_CHECK = (
    "service IS NULL OR service IN "
    "('gmail', 'calendar', 'calendar_write', 'calendar_list_read', "
    "'contacts_read', 'contacts_write', 'drive_read', 'drive_write', "
    "'drive_file')"
)


def upgrade() -> None:
    op.drop_constraint(
        "ck_pa_oauth_states_service",
        "pa_oauth_states",
        schema="public",
        type_="check",
    )
    op.create_check_constraint(
        "ck_pa_oauth_states_service",
        "pa_oauth_states",
        EXPANDED_SERVICE_CHECK,
        schema="public",
    )

    op.create_table(
        "pa_drive_selected_files",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("connection_id", sa.UUID(), nullable=False),
        sa.Column("provider_file_id", sa.String(length=200), nullable=False),
        sa.Column("name", sa.String(length=512), nullable=False),
        sa.Column("mime_type", sa.String(length=255), nullable=False),
        sa.Column(
            "modified_time",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column(
            "selected_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["auth.users.id"],
            name="fk_pa_drive_selected_files_user",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["connection_id", "user_id"],
            [
                "pa_user_connections.id",
                "pa_user_connections.user_id",
            ],
            name="fk_pa_drive_selected_files_connection_user",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "provider_file_id",
            name="uq_pa_drive_selected_files_user_file",
        ),
        schema="public",
    )
    op.create_index(
        "ix_pa_drive_selected_files_user_connection",
        "pa_drive_selected_files",
        ["user_id", "connection_id"],
        unique=False,
        schema="public",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_pa_drive_selected_files_user_connection",
        table_name="pa_drive_selected_files",
        schema="public",
    )
    op.drop_table("pa_drive_selected_files", schema="public")

    op.drop_constraint(
        "ck_pa_oauth_states_service",
        "pa_oauth_states",
        schema="public",
        type_="check",
    )
    op.create_check_constraint(
        "ck_pa_oauth_states_service",
        "pa_oauth_states",
        PREVIOUS_SERVICE_CHECK,
        schema="public",
    )
