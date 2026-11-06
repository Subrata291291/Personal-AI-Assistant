"""create durable Calendar action operation records

Revision ID: b1c4d2e8a730
Revises: 821cd57b946d
Create Date: 2026-11-06 10:08:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b1c4d2e8a730"
down_revision: Union[str, Sequence[str], None] = "821cd57b946d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pa_calendar_action_operations",
        sa.Column("proposal_id", sa.UUID(), nullable=False),
        sa.Column("operation_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("connection_id", sa.UUID(), nullable=False),
        sa.Column("idempotency_key", sa.UUID(), nullable=True),
        sa.Column("payload_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("action_type", sa.Text(), nullable=False),
        sa.Column("calendar_id", sa.Text(), nullable=False),
        sa.Column("event_id", sa.Text(), nullable=True),
        sa.Column(
            "execution_state",
            sa.String(length=32),
            server_default="pending",
            nullable=False,
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("execution_started_at", sa.DateTime(timezone=True)),
        sa.Column("execution_completed_at", sa.DateTime(timezone=True)),
        sa.Column("provider_result_classification", sa.String(length=64)),
        sa.Column(
            "reconciliation_status",
            sa.String(length=32),
            server_default="not_required",
            nullable=False,
        ),
        sa.Column("reconciled_at", sa.DateTime(timezone=True)),
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
        sa.CheckConstraint(
            "action_type IN ("
            "'calendar.create_event', 'calendar.update_event', "
            "'calendar.delete_event')",
            name="ck_pa_calendar_action_operations_action_type",
        ),
        sa.CheckConstraint(
            "execution_state IN ("
            "'pending', 'approved', 'executing', 'completed', 'failed', "
            "'expired', 'rejected', 'cancelled', 'execution_uncertain')",
            name="ck_pa_calendar_action_operations_execution_state",
        ),
        sa.CheckConstraint(
            "reconciliation_status IN ("
            "'not_required', 'pending', 'confirmed', 'not_executed', "
            "'not_found', 'conflict', 'manual_review')",
            name="ck_pa_calendar_action_operations_reconciliation_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_pa_calendar_action_operations_attempt_count",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["auth.users.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("proposal_id"),
        sa.UniqueConstraint("operation_id"),
        schema="public",
    )
    op.create_index(
        "uq_pa_calendar_action_operations_user_idempotency",
        "pa_calendar_action_operations",
        ["user_id", "idempotency_key"],
        unique=True,
        schema="public",
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.create_index(
        "ix_pa_calendar_action_operations_state_updated",
        "pa_calendar_action_operations",
        ["execution_state", "updated_at"],
        unique=False,
        schema="public",
    )
    op.execute(
        "ALTER TABLE public.pa_calendar_action_operations "
        "ENABLE ROW LEVEL SECURITY"
    )
    op.execute(
        "ALTER TABLE public.pa_calendar_action_operations "
        "FORCE ROW LEVEL SECURITY"
    )
    op.execute(
        "REVOKE ALL ON TABLE public.pa_calendar_action_operations "
        "FROM PUBLIC, anon, authenticated, service_role"
    )


def downgrade() -> None:
    has_operations = op.get_bind().execute(
        sa.text(
            "SELECT EXISTS ("
            "SELECT 1 FROM public.pa_calendar_action_operations"
            ")"
        )
    ).scalar_one()
    if has_operations:
        raise RuntimeError(
            "Calendar operation history exists; refusing to drop the "
            "idempotency journal."
        )
    op.drop_index(
        "ix_pa_calendar_action_operations_state_updated",
        table_name="pa_calendar_action_operations",
        schema="public",
    )
    op.drop_index(
        "uq_pa_calendar_action_operations_user_idempotency",
        table_name="pa_calendar_action_operations",
        schema="public",
    )
    op.drop_table("pa_calendar_action_operations", schema="public")
