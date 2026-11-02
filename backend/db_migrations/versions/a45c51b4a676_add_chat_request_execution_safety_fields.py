
"""add chat request execution safety fields

Revision ID: a45c51b4a676
Revises: 4a91c2e7b630
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a45c51b4a676"
down_revision: Union[str, None] = "4a91c2e7b630"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "pa_chat_requests",
        sa.Column(
            "request_hash",
            sa.String(length=64),
            nullable=True,
        ),
        schema="public",
    )

    op.add_column(
        "pa_chat_requests",
        sa.Column(
            "execution_started_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        schema="public",
    )


def downgrade() -> None:
    op.drop_column(
        "pa_chat_requests",
        "execution_started_at",
        schema="public",
    )

    op.drop_column(
        "pa_chat_requests",
        "request_hash",
        schema="public",
    )
