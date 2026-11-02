"""add conversation UUID server default

Revision ID: 25e053b2e67c
Revises: 6ee5bbe8410e
Create Date: 2026-11-01 18:32:00.410571
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "25e053b2e67c"
down_revision: Union[str, Sequence[str], None] = "6ee5bbe8410e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add database-generated UUID default."""

    op.alter_column(
        "pa_conversations",
        "id",
        schema="public",
        existing_type=sa.UUID(),
        server_default=sa.text("gen_random_uuid()"),
    )


def downgrade() -> None:
    """Remove database-generated UUID default."""

    op.alter_column(
        "pa_conversations",
        "id",
        schema="public",
        existing_type=sa.UUID(),
        server_default=None,
    )