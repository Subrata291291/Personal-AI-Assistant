"""allow connection-bound Calendar List consent state

Revision ID: 4f2c8a9d1e73
Revises: 2d8af64c1b73
"""

from typing import Sequence, Union

from alembic import op


revision: str = "4f2c8a9d1e73"
down_revision: Union[str, Sequence[str], None] = "2d8af64c1b73"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_pa_oauth_states_service",
        "pa_oauth_states",
        schema="public",
        type_="check",
    )
    op.create_check_constraint(
        "ck_pa_oauth_states_service",
        "pa_oauth_states",
        "service IS NULL OR service IN "
        "('gmail', 'calendar', 'calendar_write', 'calendar_list_read')",
        schema="public",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_pa_oauth_states_service",
        "pa_oauth_states",
        schema="public",
        type_="check",
    )
    op.create_check_constraint(
        "ck_pa_oauth_states_service",
        "pa_oauth_states",
        "service IS NULL OR service IN ('gmail', 'calendar', 'calendar_write')",
        schema="public",
    )
