"""Enable automatic memory by default for newly created settings rows.

Revision ID: 2d8af64c1b73
Revises: c9f42a71e6b3
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "2d8af64c1b73"
down_revision: Union[str, Sequence[str], None] = "c9f42a71e6b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "pa_user_settings",
        "memory_enabled",
        server_default="true",
        existing_type=sa.Boolean(),
        existing_nullable=False,
        schema="public",
    )


def downgrade() -> None:
    op.alter_column(
        "pa_user_settings",
        "memory_enabled",
        server_default="false",
        existing_type=sa.Boolean(),
        existing_nullable=False,
        schema="public",
    )
