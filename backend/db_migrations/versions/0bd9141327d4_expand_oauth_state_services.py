"""expand allowed OAuth state service values

Revision ID: 0bd9141327d4
Revises: 4f2c8a9d1e73
Create Date: 2026-11-15 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0bd9141327d4"
down_revision: Union[str, Sequence[str], None] = "4f2c8a9d1e73"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


PREVIOUS_SERVICE_CHECK = (
    "service IS NULL OR service IN "
    "('gmail', 'calendar', 'calendar_write', 'calendar_list_read')"
)

EXPANDED_SERVICE_CHECK = (
    "service IS NULL OR service IN "
    "('gmail', 'calendar', 'calendar_write', 'calendar_list_read', "
    "'contacts_read', 'contacts_write', 'drive_read', 'drive_write')"
)


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
        EXPANDED_SERVICE_CHECK,
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
        PREVIOUS_SERVICE_CHECK,
        schema="public",
    )
