"""create contacts action idempotency

Revision ID: 1a7f4c9d2b6e
Revises: 0bd9141327d4
Create Date: 2026-11-16 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "1a7f4c9d2b6e"
down_revision: Union[str, Sequence[str], None] = "0bd9141327d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pa_contacts_action_idempotency",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("connection_id", sa.UUID(), nullable=False),
        sa.Column("action_type", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("payload_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "normalization_version",
            sa.SmallInteger(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column("proposal_id", sa.UUID(), nullable=False),
        sa.Column(
            "proposal_storage",
            sa.String(length=32),
            server_default=sa.text("'conversation_message'"),
            nullable=False,
        ),
        sa.Column(
            "request_hash_alg",
            sa.String(length=16),
            server_default=sa.text("'sha256'"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("replay_until", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "action_type = 'contacts.create_contact'",
            name="ck_pa_contacts_action_idempotency_action_type",
        ),
        sa.CheckConstraint(
            "char_length(payload_fingerprint) = 64",
            name="ck_pa_contacts_action_idempotency_fingerprint",
        ),
        sa.CheckConstraint(
            "char_length(idempotency_key) BETWEEN 1 AND 128",
            name="ck_pa_contacts_action_idempotency_key_len",
        ),
        sa.CheckConstraint(
            "normalization_version >= 1",
            name="ck_pa_contacts_action_idempotency_norm_ver",
        ),
        sa.CheckConstraint(
            "proposal_storage = 'conversation_message'",
            name="ck_pa_contacts_action_idempotency_storage",
        ),
        sa.CheckConstraint(
            "request_hash_alg = 'sha256'",
            name="ck_pa_contacts_action_idempotency_hash_alg",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["auth.users.id"],
            name="fk_pa_contacts_action_idempotency_tenant_user",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["auth.users.id"],
            name="fk_pa_contacts_action_idempotency_user",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "user_id",
            "connection_id",
            "action_type",
            "idempotency_key",
            name="uq_pa_contacts_action_idempotency_scope_key",
        ),
        sa.UniqueConstraint(
            "proposal_id",
            name="uq_pa_contacts_action_idempotency_proposal",
        ),
        schema="public",
    )
    op.create_index(
        "ix_pa_contacts_action_idempotency_user_created",
        "pa_contacts_action_idempotency",
        ["user_id", "created_at"],
        schema="public",
    )
    op.create_index(
        "ix_pa_contacts_action_idempotency_replay_until",
        "pa_contacts_action_idempotency",
        ["replay_until"],
        schema="public",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_pa_contacts_action_idempotency_replay_until",
        table_name="pa_contacts_action_idempotency",
        schema="public",
    )
    op.drop_index(
        "ix_pa_contacts_action_idempotency_user_created",
        table_name="pa_contacts_action_idempotency",
        schema="public",
    )
    op.drop_table("pa_contacts_action_idempotency", schema="public")
