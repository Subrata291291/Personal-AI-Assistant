"""bind incremental Calendar write consent to a Google connection

Revision ID: c9f42a71e6b3
Revises: b1c4d2e8a730
Create Date: 2026-11-07 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c9f42a71e6b3"
down_revision: Union[str, Sequence[str], None] = "b1c4d2e8a730"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "pa_oauth_states",
        sa.Column("service", sa.String(length=50), nullable=True),
        schema="public",
    )
    op.add_column(
        "pa_oauth_states",
        sa.Column("target_connection_id", sa.UUID(), nullable=True),
        schema="public",
    )
    op.create_foreign_key(
        "fk_pa_oauth_states_target_connection",
        "pa_oauth_states",
        "pa_user_connections",
        ["target_connection_id"],
        ["id"],
        source_schema="public",
        referent_schema="public",
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "ck_pa_oauth_states_service",
        "pa_oauth_states",
        "service IS NULL OR service IN ('gmail', 'calendar', 'calendar_write')",
        schema="public",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_pa_oauth_states_service",
        "pa_oauth_states",
        schema="public",
        type_="check",
    )
    op.drop_constraint(
        "fk_pa_oauth_states_target_connection",
        "pa_oauth_states",
        schema="public",
        type_="foreignkey",
    )
    op.drop_column(
        "pa_oauth_states",
        "target_connection_id",
        schema="public",
    )
    op.drop_column("pa_oauth_states", "service", schema="public")
