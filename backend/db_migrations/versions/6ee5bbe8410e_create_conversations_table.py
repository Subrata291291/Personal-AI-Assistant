
"""create conversations table

Revision ID: 6ee5bbe8410e
Revises:
Create Date: 2026-11-01 17:40:58.941576
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "6ee5bbe8410e"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create user-isolated conversations table."""

    op.create_table(
        "pa_conversations",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column(
            "title",
            sa.String(length=200),
            nullable=False,
        ),
        sa.Column(
            "is_archived",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["auth.users.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        schema="public",
    )

    # Composite index for user-scoped conversation retrieval.
    op.create_index(
        "ix_pa_conversations_user_created",
        "pa_conversations",
        ["user_id", "created_at"],
        unique=False,
        schema="public",
    )

    # Enable and enforce Row Level Security.
    op.execute(
        "ALTER TABLE public.pa_conversations "
        "ENABLE ROW LEVEL SECURITY"
    )

    op.execute(
        "ALTER TABLE public.pa_conversations "
        "FORCE ROW LEVEL SECURITY"
    )

    # Allow authenticated users to access the table.
    # RLS determines which rows each user can access.
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE "
        "ON TABLE public.pa_conversations "
        "TO authenticated"
    )

    op.execute(
        """
        CREATE POLICY "Users manage their own conversations"
        ON public.pa_conversations
        FOR ALL
        TO authenticated
        USING (auth.uid() = user_id)
        WITH CHECK (auth.uid() = user_id)
        """
    )


def downgrade() -> None:
    """Remove only the application-owned conversations table."""

    op.drop_index(
        "ix_pa_conversations_user_created",
        table_name="pa_conversations",
        schema="public",
    )

    op.drop_table(
        "pa_conversations",
        schema="public",
    )
