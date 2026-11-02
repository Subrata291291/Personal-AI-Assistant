
"""create chat requests table

Revision ID: d8b6b04f83c1
Revises: 019b1ee3f7e0
Create Date: 2026-11-01
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d8b6b04f83c1"
down_revision: Union[str, Sequence[str], None] = "019b1ee3f7e0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create a secure, user-owned chat request table."""

    op.create_table(
        "pa_chat_requests",

        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),

        sa.Column(
            "user_id",
            sa.UUID(),
            nullable=False,
        ),

        sa.Column(
            "conversation_id",
            sa.UUID(),
            nullable=False,
        ),

        sa.Column(
            "idempotency_key",
            sa.UUID(),
            nullable=False,
        ),

        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'processing'"),
        ),

        sa.Column(
            "user_message_id",
            sa.UUID(),
            nullable=True,
        ),

        sa.Column(
            "assistant_message_id",
            sa.UUID(),
            nullable=True,
        ),

        sa.Column(
            "error_message",
            sa.Text(),
            nullable=True,
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

        sa.CheckConstraint(
            "status IN ('processing', 'completed', 'failed')",
            name="ck_pa_chat_requests_status",
        ),

        sa.ForeignKeyConstraint(
            ["user_id"],
            ["auth.users.id"],
            ondelete="CASCADE",
        ),

        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["public.pa_conversations.id"],
            ondelete="CASCADE",
        ),

        sa.ForeignKeyConstraint(
            ["user_message_id"],
            ["public.pa_conversation_messages.id"],
            ondelete="SET NULL",
        ),

        sa.ForeignKeyConstraint(
            ["assistant_message_id"],
            ["public.pa_conversation_messages.id"],
            ondelete="SET NULL",
        ),

        sa.PrimaryKeyConstraint("id"),

        sa.UniqueConstraint(
            "user_id",
            "conversation_id",
            "idempotency_key",
            name="uq_pa_chat_requests_user_conversation_key",
        ),

        schema="public",
    )

    # Index for retrieving requests by user and status.
    op.create_index(
        "ix_pa_chat_requests_user_status",
        "pa_chat_requests",
        ["user_id", "status"],
        unique=False,
        schema="public",
    )

    # Enable and enforce Row Level Security.
    op.execute("""
        ALTER TABLE public.pa_chat_requests
        ENABLE ROW LEVEL SECURITY
    """)

    op.execute("""
        ALTER TABLE public.pa_chat_requests
        FORCE ROW LEVEL SECURITY
    """)

    # Authenticated users can read and create their own requests.
    # UPDATE and DELETE are reserved for trusted backend operations.
    op.execute("""
        GRANT SELECT, INSERT
        ON TABLE public.pa_chat_requests
        TO authenticated
    """)

    # Read policy.
    op.execute("""
        CREATE POLICY "Users read their own chat requests"
        ON public.pa_chat_requests
        FOR SELECT
        TO authenticated
        USING (
            user_id = (SELECT auth.uid())
            AND EXISTS (
                SELECT 1
                FROM public.pa_conversations AS conversation
                WHERE conversation.id =
                    pa_chat_requests.conversation_id
                  AND conversation.user_id = (SELECT auth.uid())
            )
        )
    """)

    # Insert policy.
    op.execute("""
        CREATE POLICY "Users create their own chat requests"
        ON public.pa_chat_requests
        FOR INSERT
        TO authenticated
        WITH CHECK (
            user_id = (SELECT auth.uid())
            AND EXISTS (
                SELECT 1
                FROM public.pa_conversations AS conversation
                WHERE conversation.id =
                    pa_chat_requests.conversation_id
                  AND conversation.user_id = (SELECT auth.uid())
            )
        )
    """)


def downgrade() -> None:
    """
    Remove only the application-owned chat requests table.

    Existing conversations and messages remain untouched.
    """

    op.drop_index(
        "ix_pa_chat_requests_user_status",
        table_name="pa_chat_requests",
        schema="public",
    )

    op.drop_table(
        "pa_chat_requests",
        schema="public",
    )
