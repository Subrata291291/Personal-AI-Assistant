
"""create oauth states table

Revision ID: 023364c627d0
Revises: 895f8d00188b
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "023364c627d0"
down_revision: Union[str, Sequence[str], None] = "895f8d00188b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create OAuth states table.
    op.create_table(
        "pa_oauth_states",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        sa.Column("code_verifier_encrypted", sa.Text(), nullable=False),
        sa.Column("redirect_uri", sa.String(length=2048), nullable=False),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "consumed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
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
        sa.UniqueConstraint(
            "state_hash",
            name="uq_pa_oauth_states_state_hash",
        ),
    )

    # Create indexes.
    op.create_index(
        "ix_pa_oauth_states_expires_at",
        "pa_oauth_states",
        ["expires_at"],
        unique=False,
    )

    op.create_index(
        "ix_pa_oauth_states_user_provider",
        "pa_oauth_states",
        ["user_id", "provider"],
        unique=False,
    )

    # Enable Row Level Security.
    op.execute("""
        ALTER TABLE public.pa_oauth_states
        ENABLE ROW LEVEL SECURITY;
    """)

    # Force RLS for non-BYPASSRLS roles.
    op.execute("""
        ALTER TABLE public.pa_oauth_states
        FORCE ROW LEVEL SECURITY;
    """)

    # OAuth state is backend-only.
    # No direct access for client-facing or service roles.
    op.execute("""
        REVOKE ALL PRIVILEGES
        ON TABLE public.pa_oauth_states
        FROM anon, authenticated, service_role, PUBLIC;
    """)


def downgrade() -> None:
    # Drop indexes before dropping the table.
    op.drop_index(
        "ix_pa_oauth_states_user_provider",
        table_name="pa_oauth_states",
    )

    op.drop_index(
        "ix_pa_oauth_states_expires_at",
        table_name="pa_oauth_states",
    )

    op.drop_table("pa_oauth_states")
