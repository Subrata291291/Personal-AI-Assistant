
"""add lease fields to chat requests

Revision ID: 4a91c2e7b630
Revises: d8b6b04f83c1
Create Date: 2026-11-01
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "4a91c2e7b630"
down_revision: Union[str, None] = "d8b6b04f83c1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "pa_chat_requests",
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        schema="public",
    )

    op.add_column(
        "pa_chat_requests",
        sa.Column(
            "worker_id",
            sa.Text(),
            nullable=True,
        ),
        schema="public",
    )

    op.add_column(
        "pa_chat_requests",
        sa.Column(
            "lease_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        schema="public",
    )

    op.create_check_constraint(
        "ck_pa_chat_requests_attempt_count_nonnegative",
        "pa_chat_requests",
        "attempt_count >= 0",
        schema="public",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_pa_chat_requests_attempt_count_nonnegative",
        "pa_chat_requests",
        schema="public",
        type_="check",
    )

    op.drop_column(
        "pa_chat_requests",
        "lease_expires_at",
        schema="public",
    )

    op.drop_column(
        "pa_chat_requests",
        "worker_id",
        schema="public",
    )

    op.drop_column(
        "pa_chat_requests",
        "attempt_count",
        schema="public",
    )
