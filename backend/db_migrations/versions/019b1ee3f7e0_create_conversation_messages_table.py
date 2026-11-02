
"""create conversation messages table

Revision ID: 019b1ee3f7e0
Revises: 25e053b2e67c
Create Date: 2026-11-01 19:41:16.728641
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "019b1ee3f7e0"
down_revision: Union[str, Sequence[str], None] = "25e053b2e67c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create secure conversation messages table."""

    op.create_table(
        "pa_conversation_messages",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "conversation_id",
            sa.UUID(),
            nullable=False,
        ),
        sa.Column(
            "role",
            sa.Text(),
            nullable=False,
        ),
        sa.Column(
            "content",
            sa.Text(),
            nullable=False,
        ),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "role IN ('user', 'assistant', 'system', 'tool')",
            name="ck_pa_conversation_messages_role",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["public.pa_conversations.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        schema="public",
    )

    # Index for chronological message retrieval.
    op.create_index(
        "ix_pa_conversation_messages_conversation_created",
        "pa_conversation_messages",
        ["conversation_id", "created_at"],
        unique=False,
        schema="public",
    )

    # Enable and enforce Row Level Security.
    op.execute(
        "ALTER TABLE public.pa_conversation_messages "
        "ENABLE ROW LEVEL SECURITY"
    )

    op.execute(
        "ALTER TABLE public.pa_conversation_messages "
        "FORCE ROW LEVEL SECURITY"
    )

    # Grant CRUD permissions to authenticated users.
    op.execute("""
        GRANT SELECT, INSERT, UPDATE, DELETE
        ON TABLE public.pa_conversation_messages
        TO authenticated
    """)

    # Users can access messages only inside their own conversations.
    op.execute("""
        CREATE POLICY "Users manage messages in their own conversations"
        ON public.pa_conversation_messages
        FOR ALL
        TO authenticated
        USING (
            EXISTS (
                SELECT 1
                FROM public.pa_conversations AS conversation
                WHERE conversation.id =
                    pa_conversation_messages.conversation_id
                  AND conversation.user_id = (SELECT auth.uid())
            )
        )
        WITH CHECK (
            EXISTS (
                SELECT 1
                FROM public.pa_conversations AS conversation
                WHERE conversation.id =
                    pa_conversation_messages.conversation_id
                  AND conversation.user_id = (SELECT auth.uid())
            )
        )
    """)


def downgrade() -> None:
    """Remove only the application-owned messages table."""

    op.drop_index(
        "ix_pa_conversation_messages_conversation_created",
        table_name="pa_conversation_messages",
        schema="public",
    )

    op.drop_table(
        "pa_conversation_messages",
        schema="public",
    )
