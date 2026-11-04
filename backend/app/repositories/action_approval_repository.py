import json
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, text

from app.db.session import engine


_INSERT_PROPOSAL = text("""
    WITH proposal_clock AS (
        SELECT clock_timestamp() AS created_at
    )
    INSERT INTO public.pa_conversation_messages (
        id,
        conversation_id,
        role,
        content,
        metadata
    )
    SELECT
        CAST(:proposal_id AS uuid),
        conversation.id,
        'assistant',
        :content,
        jsonb_build_object(
            'action_approval',
            jsonb_build_object(
                'proposal_id', CAST(:proposal_id AS uuid)::text,
                'source_message_id', CAST(:source_message_id AS uuid)::text,
                'user_id', CAST(:user_id AS uuid)::text,
                'conversation_id', conversation.id::text,
                'action_type', :action_type,
                'parameters', CAST(:parameters AS jsonb),
                'connection_id', CAST(:connection_id AS uuid)::text,
                'account_email', :account_email,
                'created_at', proposal_clock.created_at::text,
                'expires_at',
                    (
                        proposal_clock.created_at
                        + make_interval(mins => :expiration_minutes)
                    )::text,
                'status', 'pending',
                'approved_at', NULL,
                'execution_started_at', NULL,
                'completed_at', NULL,
                'result', NULL
            )
        )
    FROM public.pa_conversations AS conversation
    CROSS JOIN proposal_clock
    WHERE conversation.id = CAST(:conversation_id AS uuid)
      AND conversation.user_id = CAST(:user_id AS uuid)
      AND EXISTS (
          SELECT 1
          FROM public.pa_conversation_messages AS source_message
          WHERE source_message.id = CAST(:source_message_id AS uuid)
            AND source_message.conversation_id = conversation.id
            AND source_message.role = 'assistant'
      )
    RETURNING id AS message_id, metadata, created_at
""")

_GET_PROPOSAL = text("""
    SELECT
        approval_message.id AS message_id,
        approval_message.metadata->'action_approval' AS proposal,
        connection.email AS account_email
    FROM public.pa_conversation_messages AS approval_message
    JOIN public.pa_conversations AS conversation
      ON conversation.id = approval_message.conversation_id
    LEFT JOIN public.pa_user_connections AS connection
      ON connection.id = (
          approval_message.metadata->'action_approval'->>'connection_id'
      )::uuid
     AND connection.user_id = conversation.user_id
    WHERE conversation.user_id = CAST(:user_id AS uuid)
      AND approval_message.metadata->'action_approval'->>'proposal_id'
          = CAST(:proposal_id AS uuid)::text
    LIMIT 1
""")

_LOCK_PROPOSAL_IDEMPOTENCY = text("""
    SELECT pg_advisory_xact_lock(
        hashtextextended(CAST(:lock_key AS text), 0)
    )
""")

_RECOVER_STALE_GMAIL_SEND = text("""
    UPDATE public.pa_conversation_messages AS approval_message
    SET metadata = jsonb_set(
        approval_message.metadata,
        '{action_approval}',
        (approval_message.metadata->'action_approval')
            || jsonb_build_object(
                'status', 'execution_uncertain',
                'completed_at', clock_timestamp()::text,
                'result', jsonb_build_object(
                    'outcome', 'unconfirmed',
                    'reason', 'executor_interrupted_or_timed_out'
                )
            ),
        false
    )
    FROM public.pa_conversations AS conversation
    WHERE conversation.id = approval_message.conversation_id
      AND conversation.user_id = CAST(:user_id AS uuid)
      AND approval_message.metadata->'action_approval'->>'proposal_id'
          = CAST(:proposal_id AS uuid)::text
      AND approval_message.metadata->'action_approval'->>'action_type'
          IN ('gmail.send', 'gmail.send_draft')
      AND approval_message.metadata->'action_approval'->>'status'
          = 'executing'
      AND (
          approval_message.metadata->'action_approval'
              ->>'execution_started_at'
      )::timestamptz
          <= clock_timestamp()
             - make_interval(secs => :stale_after_seconds)
    RETURNING
        approval_message.id AS message_id,
        approval_message.metadata->'action_approval' AS proposal,
        NULL::text AS account_email
""")

_INSERT_GMAIL_DRAFT_PROPOSAL = text("""
    WITH proposal_clock AS (
        SELECT clock_timestamp() AS created_at
    ),
    approval_conversation AS (
        INSERT INTO public.pa_conversations (
            id, user_id, title, is_archived
        )
        VALUES (
            CAST(:conversation_id AS uuid),
            CAST(:user_id AS uuid),
            'Gmail draft approval',
            true
        )
        RETURNING id
    ),
    source_message AS (
        INSERT INTO public.pa_conversation_messages (
            id, conversation_id, role, content, metadata
        )
        SELECT
            CAST(:source_message_id AS uuid),
            approval_conversation.id,
            'assistant',
            'Gmail draft send approval context',
            jsonb_build_object('internal_action_approval_source', true)
        FROM approval_conversation
        RETURNING id, conversation_id
    )
    INSERT INTO public.pa_conversation_messages (
        id, conversation_id, role, content, metadata
    )
    SELECT
        CAST(:proposal_id AS uuid),
        source_message.conversation_id,
        'assistant',
        'Approval proposal: gmail.send_draft',
        jsonb_build_object(
            'action_approval',
            jsonb_build_object(
                'proposal_id', CAST(:proposal_id AS uuid)::text,
                'source_message_id', source_message.id::text,
                'user_id', CAST(:user_id AS uuid)::text,
                'conversation_id', source_message.conversation_id::text,
                'action_type', 'gmail.send_draft',
                'parameters', CAST(:parameters AS jsonb),
                'connection_id', CAST(:connection_id AS uuid)::text,
                'account_email', :account_email,
                'created_at', proposal_clock.created_at::text,
                'expires_at',
                    (
                        proposal_clock.created_at
                        + make_interval(mins => :expiration_minutes)
                    )::text,
                'status', 'pending',
                'approved_at', NULL,
                'execution_started_at', NULL,
                'completed_at', NULL,
                'result', NULL
            )
        )
    FROM source_message
    CROSS JOIN proposal_clock
    RETURNING id AS message_id, metadata, created_at
""")

_LIST_PENDING = text("""
    SELECT
        approval_message.id AS message_id,
        approval_message.metadata->'action_approval' AS proposal,
        connection.email AS account_email
    FROM public.pa_conversation_messages AS approval_message
    JOIN public.pa_conversations AS conversation
      ON conversation.id = approval_message.conversation_id
    LEFT JOIN public.pa_user_connections AS connection
      ON connection.id = (
          approval_message.metadata->'action_approval'->>'connection_id'
      )::uuid
     AND connection.user_id = conversation.user_id
    WHERE conversation.user_id = CAST(:user_id AS uuid)
      AND (
          CAST(:conversation_id AS uuid) IS NULL
          OR conversation.id = CAST(:conversation_id AS uuid)
      )
      AND approval_message.metadata->'action_approval'->>'status' = 'pending'
      AND (
          approval_message.metadata->'action_approval'->>'expires_at'
      )::timestamptz > clock_timestamp()
    ORDER BY approval_message.created_at DESC
    LIMIT 100
""")

_EXPIRE_PENDING = text("""
    UPDATE public.pa_conversation_messages AS approval_message
    SET metadata = jsonb_set(
        approval_message.metadata,
        '{action_approval}',
        (approval_message.metadata->'action_approval')
            || jsonb_build_object(
                'status', 'expired',
                'completed_at', clock_timestamp()::text
            ),
        false
    )
    FROM public.pa_conversations AS conversation
    WHERE conversation.id = approval_message.conversation_id
      AND conversation.user_id = CAST(:user_id AS uuid)
      AND (
          CAST(:conversation_id AS uuid) IS NULL
          OR conversation.id = CAST(:conversation_id AS uuid)
      )
      AND approval_message.metadata->'action_approval'->>'status' = 'pending'
      AND (
          approval_message.metadata->'action_approval'->>'expires_at'
      )::timestamptz <= clock_timestamp()
""")

_TRANSITION = text("""
    UPDATE public.pa_conversation_messages AS approval_message
    SET metadata = jsonb_set(
        approval_message.metadata,
        '{action_approval}',
        (approval_message.metadata->'action_approval')
            || CAST(:patch AS jsonb)
            || CASE
                WHEN :timestamp_field IS NULL THEN '{}'::jsonb
                ELSE jsonb_build_object(
                    CAST(:timestamp_field AS text),
                    clock_timestamp()::text
                )
            END,
        false
    )
    FROM public.pa_conversations AS conversation
    WHERE conversation.id = approval_message.conversation_id
      AND conversation.user_id = CAST(:user_id AS uuid)
      AND approval_message.metadata->'action_approval'->>'proposal_id'
          = CAST(:proposal_id AS uuid)::text
      AND approval_message.metadata->'action_approval'->>'status'
          = :expected_status
      AND (
          :expiry_mode = 'any'
          OR (
              :expiry_mode = 'valid'
              AND (
                  approval_message.metadata->'action_approval'->>'expires_at'
              )::timestamptz > clock_timestamp()
          )
          OR (
              :expiry_mode = 'expired'
              AND (
                  approval_message.metadata->'action_approval'->>'expires_at'
              )::timestamptz <= clock_timestamp()
          )
      )
    RETURNING
        approval_message.id AS message_id,
        approval_message.metadata->'action_approval' AS proposal,
        NULL::text AS account_email
""")


class ActionApprovalRepository:
    """Durable approval records stored as dedicated conversation messages."""

    def __init__(self, database_engine: Engine | None = None):
        self.engine = database_engine or engine

    def create_proposal(
        self,
        *,
        proposal_id: UUID,
        user_id: UUID,
        conversation_id: UUID,
        source_message_id: UUID,
        action_type: str,
        parameters: dict[str, Any],
        connection_id: UUID,
        account_email: str | None,
        expiration_minutes: int,
    ) -> dict[str, Any] | None:
        params = {
            "proposal_id": str(proposal_id),
            "user_id": str(user_id),
            "conversation_id": str(conversation_id),
            "source_message_id": str(source_message_id),
            "action_type": action_type,
            "parameters": json.dumps(
                parameters,
                sort_keys=True,
                separators=(",", ":"),
            ),
            "connection_id": str(connection_id),
            "account_email": account_email,
            "expiration_minutes": expiration_minutes,
            "content": f"Approval proposal: {action_type}",
        }
        with self.engine.begin() as connection:
            row = (
                connection.execute(_INSERT_PROPOSAL, params)
                .mappings()
                .first()
            )
            if row is None:
                return None
            return self._proposal_row(row, account_email)

    def create_gmail_draft_proposal(
        self,
        *,
        proposal_id: UUID,
        source_message_id: UUID,
        conversation_id: UUID,
        user_id: UUID,
        parameters: dict[str, Any],
        connection_id: UUID,
        account_email: str | None,
        expiration_minutes: int,
    ) -> dict[str, Any]:
        params = {
            "proposal_id": str(proposal_id),
            "source_message_id": str(source_message_id),
            "conversation_id": str(conversation_id),
            "user_id": str(user_id),
            "parameters": json.dumps(
                parameters,
                sort_keys=True,
                separators=(",", ":"),
            ),
            "connection_id": str(connection_id),
            "account_email": account_email,
            "expiration_minutes": expiration_minutes,
        }
        with self.engine.begin() as connection:
            connection.execute(
                _LOCK_PROPOSAL_IDEMPOTENCY,
                {"lock_key": str(proposal_id)},
            )
            existing = (
                connection.execute(
                    _GET_PROPOSAL,
                    {
                        "user_id": str(user_id),
                        "proposal_id": str(proposal_id),
                    },
                )
                .mappings()
                .first()
            )
            if existing is not None:
                return self._proposal_row(existing)
            row = (
                connection.execute(
                    _INSERT_GMAIL_DRAFT_PROPOSAL,
                    params,
                )
                .mappings()
                .first()
            )
        if row is None:
            raise RuntimeError("Gmail draft approval could not be stored.")
        return self._proposal_row(row, account_email)

    def get_gmail_draft_proposal(
        self,
        *,
        user_id: UUID,
        proposal_id: UUID,
    ) -> dict[str, Any] | None:
        with self.engine.begin() as connection:
            connection.execute(
                _LOCK_PROPOSAL_IDEMPOTENCY,
                {"lock_key": str(proposal_id)},
            )
            row = (
                connection.execute(
                    _GET_PROPOSAL,
                    {
                        "user_id": str(user_id),
                        "proposal_id": str(proposal_id),
                    },
                )
                .mappings()
                .first()
            )
        return self._proposal_row(row) if row is not None else None

    def get_proposal(
        self,
        user_id: UUID,
        proposal_id: UUID,
    ) -> dict[str, Any] | None:
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    _GET_PROPOSAL,
                    {
                        "user_id": str(user_id),
                        "proposal_id": str(proposal_id),
                    },
                )
                .mappings()
                .first()
            )
        return self._proposal_row(row) if row is not None else None

    def recover_stale_gmail_send(
        self,
        user_id: UUID,
        proposal_id: UUID,
        stale_after_seconds: int,
    ) -> dict[str, Any] | None:
        with self.engine.begin() as connection:
            row = (
                connection.execute(
                    _RECOVER_STALE_GMAIL_SEND,
                    {
                        "user_id": str(user_id),
                        "proposal_id": str(proposal_id),
                        "stale_after_seconds": stale_after_seconds,
                    },
                )
                .mappings()
                .first()
            )
        return self._proposal_row(row) if row is not None else None

    def list_pending(
        self,
        user_id: UUID,
        conversation_id: UUID | None = None,
    ) -> list[dict[str, Any]]:
        params = {
            "user_id": str(user_id),
            "conversation_id": (
                str(conversation_id) if conversation_id else None
            ),
        }
        with self.engine.begin() as connection:
            connection.execute(_EXPIRE_PENDING, params)
            rows = (
                connection.execute(_LIST_PENDING, params)
                .mappings()
                .all()
            )
        return [self._proposal_row(row) for row in rows]

    def transition(
        self,
        *,
        user_id: UUID,
        proposal_id: UUID,
        expected_status: str,
        new_status: str,
        expiry_mode: str = "any",
        timestamp_field: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        patch: dict[str, Any] = {"status": new_status}
        if result is not None:
            patch["result"] = result

        with self.engine.begin() as connection:
            row = (
                connection.execute(
                    _TRANSITION,
                    {
                        "user_id": str(user_id),
                        "proposal_id": str(proposal_id),
                        "expected_status": expected_status,
                        "expiry_mode": expiry_mode,
                        "timestamp_field": timestamp_field,
                        "patch": json.dumps(
                            patch,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    },
                )
                .mappings()
                .first()
            )
        return self._proposal_row(row) if row is not None else None

    @staticmethod
    def _proposal_row(
        row: Any,
        account_email: str | None = None,
    ) -> dict[str, Any]:
        proposal = row["proposal"] if "proposal" in row else (
            row["metadata"].get("action_approval")
        )
        proposal = dict(proposal)
        for field in (
            "proposal_id",
            "source_message_id",
            "user_id",
            "conversation_id",
            "connection_id",
        ):
            proposal[field] = UUID(str(proposal[field]))
        for field in (
            "created_at",
            "expires_at",
            "approved_at",
            "execution_started_at",
            "completed_at",
        ):
            value = proposal.get(field)
            if isinstance(value, str):
                proposal[field] = datetime.fromisoformat(
                    value.replace("Z", "+00:00")
                )
        current_account_email = (
            row.get("account_email")
            if "account_email" in row
            else None
        )
        return {
            **proposal,
            "message_id": UUID(str(row["message_id"])),
            "account_email": (
                current_account_email
                or proposal.get("account_email")
                or account_email
            ),
        }
