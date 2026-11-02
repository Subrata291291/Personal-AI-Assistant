
"""add chat request id to messages

Revision ID: 6f37d9a42afe
Revises: a45c51b4a676
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "6f37d9a42afe"
down_revision: Union[str, None] = "a45c51b4a676"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "pa_conversation_messages",
        sa.Column(
            "chat_request_id",
            sa.UUID(),
            nullable=True,
        ),
        schema="public",
    )

    op.create_foreign_key(
        "fk_pa_messages_chat_request",
        "pa_conversation_messages",
        "pa_chat_requests",
        ["chat_request_id"],
        ["id"],
        source_schema="public",
        referent_schema="public",
        ondelete="SET NULL",
    )

    op.create_index(
        "uq_pa_messages_chat_request_role",
        "pa_conversation_messages",
        ["chat_request_id", "role"],
        unique=True,
        schema="public",
        postgresql_where=sa.text("chat_request_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_pa_messages_chat_request_role",
        table_name="pa_conversation_messages",
        schema="public",
    )

    op.drop_constraint(
        "fk_pa_messages_chat_request",
        "pa_conversation_messages",
        schema="public",
        type_="foreignkey",
    )

    op.drop_column(
        "pa_conversation_messages",
        "chat_request_id",
        schema="public",
    )
