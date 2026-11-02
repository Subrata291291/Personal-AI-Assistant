"""create oauth credentials table

Revision ID: 895f8d00188b
Revises: a73faa34ca23
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "895f8d00188b"
down_revision: Union[str, Sequence[str], None] = "a73faa34ca23"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # First, create the unique constraint required by the composite FK.
    op.create_unique_constraint(
        "uq_pa_user_connections_id_user",
        "pa_user_connections",
        ["id", "user_id"],
    )

    # Create OAuth credentials table.
    op.create_table(
        "pa_oauth_credentials",
        sa.Column("connection_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("access_token_encrypted", sa.Text(), nullable=False),
        sa.Column("refresh_token_encrypted", sa.Text(), nullable=True),
        sa.Column(
            "token_type",
            sa.String(length=50),
            server_default="Bearer",
            nullable=False,
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "scopes",
            postgresql.ARRAY(sa.String(length=255)),
            server_default="{}",
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
            ["connection_id", "user_id"],
            [
                "pa_user_connections.id",
                "pa_user_connections.user_id",
            ],
            name="fk_pa_oauth_credentials_connection_user",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("connection_id"),
    )

    # Enable Row Level Security.
    op.execute("""
        ALTER TABLE public.pa_oauth_credentials
        ENABLE ROW LEVEL SECURITY;
    """)

    # Force RLS for non-BYPASSRLS roles, including the table owner.
    op.execute("""
        ALTER TABLE public.pa_oauth_credentials
        FORCE ROW LEVEL SECURITY;
    """)

    # Explicitly revoke direct access from client-facing roles.
    op.execute("""
        REVOKE ALL PRIVILEGES
        ON TABLE public.pa_oauth_credentials
        FROM anon, authenticated, PUBLIC;
    """)

    # No direct policies or grants for authenticated users.
    # OAuth credentials are accessed only by the trusted backend role.


def downgrade() -> None:
    # Drop dependent table before its referenced unique constraint.
    op.drop_table("pa_oauth_credentials")

    op.drop_constraint(
        "uq_pa_user_connections_id_user",
        "pa_user_connections",
        type_="unique",
    )