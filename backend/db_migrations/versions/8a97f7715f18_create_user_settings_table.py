"""create user settings table

Revision ID: 8a97f7715f18
Revises: 90ed5bf7013b
Create Date: 2026-11-02 12:22:14.202212
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# Revision identifiers.
revision: str = "8a97f7715f18"
down_revision: Union[str, Sequence[str], None] = "90ed5bf7013b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create user settings table with Row Level Security."""

    op.create_table(
        "pa_user_settings",

        sa.Column(
            "user_id",
            sa.UUID(),
            nullable=False,
        ),

        sa.Column(
            "memory_enabled",
            sa.Boolean(),
            server_default="false",
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

        sa.PrimaryKeyConstraint("user_id"),

        schema="public",
    )

    # Enable and enforce Row Level Security.
    op.execute(
        "ALTER TABLE public.pa_user_settings ENABLE ROW LEVEL SECURITY"
    )

    op.execute(
        "ALTER TABLE public.pa_user_settings FORCE ROW LEVEL SECURITY"
    )

    # Users can only read their own settings.
    op.execute("""
        CREATE POLICY pa_user_settings_select_own
        ON public.pa_user_settings
        FOR SELECT
        TO authenticated
        USING (auth.uid() = user_id)
    """)

    # Users can only insert their own settings.
    op.execute("""
        CREATE POLICY pa_user_settings_insert_own
        ON public.pa_user_settings
        FOR INSERT
        TO authenticated
        WITH CHECK (auth.uid() = user_id)
    """)

    # Users can only update their own settings.
    op.execute("""
        CREATE POLICY pa_user_settings_update_own
        ON public.pa_user_settings
        FOR UPDATE
        TO authenticated
        USING (auth.uid() = user_id)
        WITH CHECK (auth.uid() = user_id)
    """)

    # Users can only delete their own settings.
    op.execute("""
        CREATE POLICY pa_user_settings_delete_own
        ON public.pa_user_settings
        FOR DELETE
        TO authenticated
        USING (auth.uid() = user_id)
    """)

    # Grant permissions to authenticated users.
    op.execute("""
        GRANT SELECT, INSERT, UPDATE, DELETE
        ON TABLE public.pa_user_settings
        TO authenticated
    """)


def downgrade() -> None:
    """Remove only the user settings table."""

    op.drop_table(
        "pa_user_settings",
        schema="public",
    )