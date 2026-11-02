
"""create persistent memories table

Revision ID: 90ed5bf7013b
Revises: 6f37d9a42afe
Create Date: 2026-11-02 10:20:20.395774

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from pgvector.sqlalchemy import VECTOR


# Revision identifiers.
revision: str = "90ed5bf7013b"
down_revision: Union[str, Sequence[str], None] = "6f37d9a42afe"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create persistent memories table."""

    op.create_table(
        "pa_memories",

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
            "memory_type",
            sa.Text(),
            nullable=False,
        ),

        sa.Column(
            "content",
            sa.Text(),
            nullable=False,
        ),

        sa.Column(
            "embedding",
            VECTOR(768),
            nullable=True,
        ),

        sa.Column(
            "importance",
            sa.SmallInteger(),
            server_default="5",
            nullable=False,
        ),

        sa.Column(
            "confidence",
            sa.Float(),
            server_default="1.0",
            nullable=False,
        ),

        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),

        sa.Column(
            "source_conversation_id",
            sa.UUID(),
            nullable=True,
        ),

        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
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
            "memory_type IN "
            "('long_term', 'episodic', 'semantic', 'procedural')",
            name="ck_pa_memories_memory_type",
        ),

        sa.CheckConstraint(
            "importance BETWEEN 1 AND 10",
            name="ck_pa_memories_importance",
        ),

        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_pa_memories_confidence",
        ),

        sa.ForeignKeyConstraint(
            ["user_id"],
            ["auth.users.id"],
            ondelete="CASCADE",
        ),

        sa.ForeignKeyConstraint(
            ["source_conversation_id"],
            ["pa_conversations.id"],
            ondelete="SET NULL",
        ),

        sa.PrimaryKeyConstraint("id"),
        schema="public",
    )

    # User and memory type lookup.
    op.create_index(
        "ix_pa_memories_user_type",
        "pa_memories",
        ["user_id", "memory_type"],
        unique=False,
        schema="public",
    )

    # User memory timeline.
    op.create_index(
        "ix_pa_memories_user_created",
        "pa_memories",
        ["user_id", "created_at"],
        unique=False,
        schema="public",
    )

    # Source conversation lookup.
    op.create_index(
        "ix_pa_memories_source_conversation",
        "pa_memories",
        ["source_conversation_id"],
        unique=False,
        schema="public",
    )

    # Enable and enforce Row Level Security.
    op.execute(
        "ALTER TABLE public.pa_memories ENABLE ROW LEVEL SECURITY"
    )

    op.execute(
        "ALTER TABLE public.pa_memories FORCE ROW LEVEL SECURITY"
    )

    # Authenticated users can only read their own memories.
    op.execute("""
        CREATE POLICY pa_memories_select_own
        ON public.pa_memories
        FOR SELECT
        TO authenticated
        USING (auth.uid() = user_id)
    """)

    # Authenticated users can only insert their own memories.
    op.execute("""
        CREATE POLICY pa_memories_insert_own
        ON public.pa_memories
        FOR INSERT
        TO authenticated
        WITH CHECK (auth.uid() = user_id)
    """)

    # Authenticated users can only update their own memories.
    op.execute("""
        CREATE POLICY pa_memories_update_own
        ON public.pa_memories
        FOR UPDATE
        TO authenticated
        USING (auth.uid() = user_id)
        WITH CHECK (auth.uid() = user_id)
    """)

    # Authenticated users can only delete their own memories.
    op.execute("""
        CREATE POLICY pa_memories_delete_own
        ON public.pa_memories
        FOR DELETE
        TO authenticated
        USING (auth.uid() = user_id)
    """)

    # Grant table permissions to authenticated users.
    op.execute("""
        GRANT SELECT, INSERT, UPDATE, DELETE
        ON TABLE public.pa_memories
        TO authenticated
    """)


def downgrade() -> None:
    """
    Remove only the persistent memories table.

    Existing conversations, chat requests and messages
    must remain untouched.
    """

    op.drop_table(
        "pa_memories",
        schema="public",
    )
