import json
import re
import traceback
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, text

from app.core.logging import get_logger
from app.core.request_metrics import request_id
from app.db.session import engine
from app.schemas.action_approval import ActionApprovalProposal
from app.services.calendar_action_identity import (
    CalendarOperationConflictError,
    validate_idempotency_fingerprint_reuse,
    verify_calendar_proposal_integrity,
)


logger = get_logger(__name__)


class ApprovalReplacementConflictError(RuntimeError):
    """Raised when a pending proposal cannot be safely superseded."""


_EMAIL_PATTERN = re.compile(
    r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
    re.IGNORECASE,
)
_QUOTED_VALUE_PATTERN = re.compile(r"""(['"]).*?\1""")
_KEY_VALUE_PATTERN = re.compile(r"Key \(([^)]*)\)=\((.*?)\)")
_INVALID_INPUT_VALUE_PATTERN = re.compile(
    r"""(invalid input syntax for type [^:]+:\s*)(['"]).*?\2""",
    re.IGNORECASE,
)


def _sanitize_postgres_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = _KEY_VALUE_PATTERN.sub(r"Key (\1)=([REDACTED])", value)
    value = _INVALID_INPUT_VALUE_PATTERN.sub(r"\1[REDACTED]", value)
    value = _QUOTED_VALUE_PATTERN.sub("[REDACTED]", value)
    value = _EMAIL_PATTERN.sub("[REDACTED_EMAIL]", value)
    value = re.sub(
        r"(?i)\b(bearer\s+|(?:access|refresh)?_?token\s*[:=]\s*)"
        r"[^\s,;]+",
        r"\1[REDACTED]",
        value,
    )
    return value.replace("\r", " ").replace("\n", " ")


def _safe_exception_traceback(exc: BaseException) -> str:
    """Keep the complete exception chain and frames, but not exception text."""
    formatted: list[str] = []
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if current.__traceback__ is not None:
            formatted.extend(traceback.format_tb(current.__traceback__))
        exception_class = (
            f"{type(current).__module__}.{type(current).__qualname__}"
        )
        formatted.append(f"{exception_class}: [message redacted]")
        if current.__cause__ is not None:
            formatted.append("Caused by:")
            current = current.__cause__
        elif (
            current.__context__ is not None
            and not current.__suppress_context__
        ):
            formatted.append("During handling of the above exception:")
            current = current.__context__
        else:
            current = None
    return "".join(formatted)


def _database_exception_diagnostics(exc: Exception) -> dict[str, Any]:
    original = getattr(exc, "orig", None)
    diagnostic = getattr(original, "diag", None)
    statement = getattr(exc, "statement", None)
    return {
        "exception_class": (
            f"{type(exc).__module__}.{type(exc).__qualname__}"
        ),
        "dbapi_exception_class": (
            f"{type(original).__module__}.{type(original).__qualname__}"
            if original is not None
            else None
        ),
        "sqlstate": (
            getattr(original, "sqlstate", None)
            or getattr(original, "pgcode", None)
        ),
        "postgres_message": _sanitize_postgres_text(
            getattr(diagnostic, "message_primary", None),
        ),
        "postgres_detail": _sanitize_postgres_text(
            getattr(diagnostic, "message_detail", None),
        ),
        "postgres_hint": _sanitize_postgres_text(
            getattr(diagnostic, "message_hint", None),
        ),
        "constraint": _sanitize_postgres_text(
            getattr(diagnostic, "constraint_name", None),
        ),
        "table": _sanitize_postgres_text(
            getattr(diagnostic, "table_name", None),
        ),
        "column": _sanitize_postgres_text(
            getattr(diagnostic, "column_name", None),
        ),
        "sql_statement": statement if isinstance(statement, str) else None,
        "sql_parameters": "[REDACTED]",
        "traceback": _safe_exception_traceback(exc),
    }


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
                'action_type', CAST(:action_type AS text),
                'parameters', CAST(:parameters AS jsonb),
                'connection_id', CAST(:connection_id AS uuid)::text,
                'account_email', CAST(:account_email AS text),
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
            ) || COALESCE(CAST(:calendar_integrity AS jsonb), '{}'::jsonb)
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

_INSERT_CALENDAR_OPERATION = text("""
    INSERT INTO public.pa_calendar_action_operations (
        proposal_id,
        operation_id,
        user_id,
        connection_id,
        idempotency_key,
        payload_fingerprint,
        action_type,
        calendar_id,
        event_id
    )
    VALUES (
        CAST(:proposal_id AS uuid),
        CAST(:operation_id AS uuid),
        CAST(:user_id AS uuid),
        CAST(:connection_id AS uuid),
        CAST(:idempotency_key AS uuid),
        CAST(:payload_fingerprint AS text),
        CAST(:action_type AS text),
        CAST(:calendar_id AS text),
        CAST(:event_id AS text)
    )
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
                'account_email', CAST(:account_email AS text),
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
      AND approval_message.metadata->'action_approval'->>'status'
          IN ('pending', 'approved')
      AND (
          approval_message.metadata->'action_approval'->>'expires_at'
      )::timestamptz > clock_timestamp()
    ORDER BY approval_message.created_at DESC
    LIMIT 100
""")

_EXPIRE_PENDING = text("""
    WITH expired_proposals AS (
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
    RETURNING
        approval_message.metadata->'action_approval'->>'proposal_id'
            AS proposal_id
    )
    UPDATE public.pa_calendar_action_operations AS operation
    SET execution_state = 'expired',
        execution_completed_at = clock_timestamp(),
        updated_at = clock_timestamp()
    FROM expired_proposals
    WHERE operation.proposal_id = CAST(
        expired_proposals.proposal_id AS uuid
    )
      AND operation.execution_state IN ('pending', 'approved')
""")

_TRANSITION = text("""
    UPDATE public.pa_conversation_messages AS approval_message
    SET metadata = jsonb_set(
        approval_message.metadata,
        '{action_approval}',
        (approval_message.metadata->'action_approval')
            || CAST(:patch AS jsonb)
            || CASE
                WHEN CAST(:timestamp_field AS text) IS NULL THEN '{}'::jsonb
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

_SYNC_CALENDAR_APPROVAL_STATE = text("""
    UPDATE public.pa_calendar_action_operations
    SET execution_state = CAST(:new_state AS text),
        execution_completed_at = CASE
            WHEN :new_state IN ('rejected', 'expired')
                THEN clock_timestamp()
            ELSE execution_completed_at
        END,
        updated_at = clock_timestamp()
    WHERE user_id = CAST(:user_id AS uuid)
      AND proposal_id = CAST(:proposal_id AS uuid)
      AND execution_state = CAST(:expected_state AS text)
""")

_LOCK_CALENDAR_APPROVAL_FOR_EXECUTION = text("""
    SELECT
        approval_message.id AS message_id,
        approval_message.metadata->'action_approval' AS proposal,
        NULL::text AS account_email,
        operation.*,
        clock_timestamp() AS database_now
    FROM public.pa_conversation_messages AS approval_message
    JOIN public.pa_conversations AS conversation
      ON conversation.id = approval_message.conversation_id
    JOIN public.pa_calendar_action_operations AS operation
      ON operation.proposal_id = approval_message.id
     AND operation.user_id = conversation.user_id
    WHERE conversation.user_id = CAST(:user_id AS uuid)
      AND operation.proposal_id = CAST(:proposal_id AS uuid)
    FOR UPDATE OF approval_message, operation
""")

_EXPIRE_CALENDAR_APPROVAL_CLAIM = text("""
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
    WHERE approval_message.id = CAST(:proposal_id AS uuid)
      AND approval_message.metadata->'action_approval'->>'status'
          = 'approved'
      AND (
          approval_message.metadata->'action_approval'->>'expires_at'
      )::timestamptz <= clock_timestamp()
    RETURNING approval_message.id
""")

_EXPIRE_CALENDAR_OPERATION_CLAIM = text("""
    UPDATE public.pa_calendar_action_operations
    SET execution_state = 'expired',
        execution_completed_at = clock_timestamp(),
        updated_at = clock_timestamp()
    WHERE user_id = CAST(:user_id AS uuid)
      AND proposal_id = CAST(:proposal_id AS uuid)
      AND execution_state = 'approved'
    RETURNING proposal_id
""")

_CLAIM_CALENDAR_APPROVAL = text("""
    UPDATE public.pa_conversation_messages AS approval_message
    SET metadata = jsonb_set(
        approval_message.metadata,
        '{action_approval}',
        (approval_message.metadata->'action_approval')
            || jsonb_build_object(
                'status', 'executing',
                'execution_started_at', clock_timestamp()::text
            ),
        false
    )
    FROM public.pa_conversations AS conversation
    WHERE conversation.id = approval_message.conversation_id
      AND conversation.user_id = CAST(:user_id AS uuid)
      AND approval_message.id = CAST(:proposal_id AS uuid)
      AND approval_message.metadata->'action_approval'->>'status'
          = 'approved'
      AND (
          approval_message.metadata->'action_approval'->>'expires_at'
      )::timestamptz > clock_timestamp()
    RETURNING
        approval_message.id AS message_id,
        approval_message.metadata->'action_approval' AS proposal,
        NULL::text AS account_email
""")

_CLAIM_CALENDAR_OPERATION = text("""
    UPDATE public.pa_calendar_action_operations
    SET execution_state = 'executing',
        attempt_count = attempt_count + 1,
        execution_started_at = clock_timestamp(),
        execution_completed_at = NULL,
        updated_at = clock_timestamp()
    WHERE user_id = CAST(:user_id AS uuid)
      AND proposal_id = CAST(:proposal_id AS uuid)
      AND execution_state = 'approved'
    RETURNING proposal_id
""")

_LOCK_CALENDAR_EXECUTION_STATE = text("""
    SELECT
        approval_message.id AS message_id,
        approval_message.metadata->'action_approval' AS proposal,
        operation.execution_state
    FROM public.pa_conversation_messages AS approval_message
    JOIN public.pa_conversations AS conversation
      ON conversation.id = approval_message.conversation_id
    JOIN public.pa_calendar_action_operations AS operation
      ON operation.proposal_id = approval_message.id
     AND operation.user_id = conversation.user_id
    WHERE conversation.user_id = CAST(:user_id AS uuid)
      AND operation.proposal_id = CAST(:proposal_id AS uuid)
    FOR UPDATE OF approval_message, operation
""")

_UPDATE_CALENDAR_APPROVAL_RESULT = text("""
    UPDATE public.pa_conversation_messages
    SET metadata = jsonb_set(
        metadata,
        '{action_approval}',
        (metadata->'action_approval')
            || CAST(:approval_patch AS jsonb)
            || jsonb_build_object('completed_at', clock_timestamp()::text),
        false
    )
    WHERE id = CAST(:proposal_id AS uuid)
      AND metadata->'action_approval'->>'status'
          = CAST(:expected_state AS text)
    RETURNING metadata->'action_approval' AS proposal
""")

_UPDATE_CALENDAR_OPERATION_RESULT = text("""
    UPDATE public.pa_calendar_action_operations
    SET execution_state = CAST(:new_state AS text),
        execution_completed_at = clock_timestamp(),
        provider_result_classification = CAST(
            :provider_result_classification AS text
        ),
        reconciliation_status = CAST(:reconciliation_status AS text),
        reconciled_at = CASE
            WHEN :reconciliation_status = 'confirmed'
                THEN clock_timestamp()
            ELSE reconciled_at
        END,
        updated_at = clock_timestamp()
    WHERE user_id = CAST(:user_id AS uuid)
      AND proposal_id = CAST(:proposal_id AS uuid)
      AND execution_state = CAST(:expected_state AS text)
    RETURNING proposal_id
""")

_UPDATE_UNCERTAIN_RECONCILIATION = text("""
    UPDATE public.pa_calendar_action_operations
    SET reconciliation_status = CAST(:reconciliation_status AS text),
        reconciled_at = CASE
            WHEN :reconciliation_status = 'manual_review'
                THEN clock_timestamp()
            ELSE reconciled_at
        END,
        updated_at = clock_timestamp()
    WHERE user_id = CAST(:user_id AS uuid)
      AND proposal_id = CAST(:proposal_id AS uuid)
      AND execution_state = 'execution_uncertain'
    RETURNING proposal_id
""")

_STALE_CALENDAR_EXECUTION_CHECK = text("""
    SELECT
        approval_message.id AS message_id,
        approval_message.metadata->'action_approval' AS proposal,
        operation.execution_state,
        operation.execution_started_at,
        clock_timestamp() AS database_now
    FROM public.pa_conversation_messages AS approval_message
    JOIN public.pa_conversations AS conversation
      ON conversation.id = approval_message.conversation_id
    JOIN public.pa_calendar_action_operations AS operation
      ON operation.proposal_id = approval_message.id
     AND operation.user_id = conversation.user_id
    WHERE conversation.user_id = CAST(:user_id AS uuid)
      AND operation.proposal_id = CAST(:proposal_id AS uuid)
    FOR UPDATE OF approval_message, operation
""")

_MARK_STALE_CALENDAR_APPROVAL_UNCERTAIN = text("""
    UPDATE public.pa_conversation_messages
    SET metadata = jsonb_set(
        metadata,
        '{action_approval}',
        (metadata->'action_approval')
            || jsonb_build_object(
                'status', 'execution_uncertain',
                'completed_at', clock_timestamp()::text,
                'result', jsonb_build_object(
                    'outcome', 'unconfirmed',
                    'reason', 'worker_interrupted'
                )
            ),
        false
    )
    WHERE id = CAST(:proposal_id AS uuid)
      AND metadata->'action_approval'->>'status' = 'executing'
    RETURNING
        id AS message_id,
        metadata->'action_approval' AS proposal,
        NULL::text AS account_email
""")

_MARK_STALE_CALENDAR_OPERATION_UNCERTAIN = text("""
    UPDATE public.pa_calendar_action_operations
    SET execution_state = 'execution_uncertain',
        execution_completed_at = clock_timestamp(),
        provider_result_classification = 'outcome_unknown',
        reconciliation_status = 'pending',
        updated_at = clock_timestamp()
    WHERE user_id = CAST(:user_id AS uuid)
      AND proposal_id = CAST(:proposal_id AS uuid)
      AND execution_state = 'executing'
    RETURNING proposal_id
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
        calendar_operation: dict[str, Any] | None = None,
        supersedes_proposal_id: UUID | None = None,
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
            "calendar_integrity": (
                json.dumps(
                    {
                        "integrity_schema_version": (
                            calendar_operation["integrity_schema_version"]
                        ),
                        "payload_fingerprint": (
                            calendar_operation["payload_fingerprint"]
                        ),
                        "operation_id": calendar_operation["operation_id"],
                        "calendar_event_id": calendar_operation["event_id"],
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                if calendar_operation is not None
                else None
            ),
        }
        with self.engine.begin() as connection:
            if calendar_operation is not None:
                connection.execute(
                    _LOCK_PROPOSAL_IDEMPOTENCY,
                    {
                        "lock_key": (
                            "calendar-create:"
                            f"{calendar_operation['user_id']}:"
                            f"{calendar_operation['proposal_id']}"
                        ),
                    },
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
                    existing_row = self._proposal_row(existing, account_email)
                    if (
                        existing_row["payload_fingerprint"]
                        != calendar_operation["payload_fingerprint"]
                        or existing_row["operation_id"]
                        != UUID(str(calendar_operation["operation_id"]))
                    ):
                        raise CalendarOperationConflictError(
                            "This Calendar proposal ID is already bound to "
                            "different event details."
                        )
                    return existing_row
            if supersedes_proposal_id is not None:
                superseded = (
                    connection.execute(
                        _GET_PROPOSAL,
                        {
                            "user_id": str(user_id),
                            "proposal_id": str(supersedes_proposal_id),
                        },
                    )
                    .mappings()
                    .first()
                )
                if superseded is None:
                    raise ApprovalReplacementConflictError(
                        "The pending Calendar proposal could not be found."
                    )
                superseded_row = self._proposal_row(
                    superseded,
                    account_email,
                )
                if (
                    superseded_row["status"] != "pending"
                    or superseded_row["action_type"]
                    != "calendar.create_event"
                    or superseded_row["conversation_id"] != conversation_id
                    or superseded_row["connection_id"] != connection_id
                ):
                    raise ApprovalReplacementConflictError(
                        "The pending Calendar proposal is no longer eligible "
                        "for replacement."
                    )
                transitioned = (
                    connection.execute(
                        _TRANSITION,
                        {
                            "user_id": str(user_id),
                            "proposal_id": str(supersedes_proposal_id),
                            "expected_status": "pending",
                            "expiry_mode": "valid",
                            "timestamp_field": "completed_at",
                            "patch": json.dumps(
                                {"status": "rejected"},
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                        },
                    )
                    .mappings()
                    .first()
                )
                if transitioned is None:
                    raise ApprovalReplacementConflictError(
                        "The pending Calendar proposal changed before it "
                        "could be replaced."
                    )
                synchronized = connection.execute(
                    _SYNC_CALENDAR_APPROVAL_STATE,
                    {
                        "user_id": str(user_id),
                        "proposal_id": str(supersedes_proposal_id),
                        "expected_state": "pending",
                        "new_state": "rejected",
                    },
                )
                if synchronized.rowcount != 1:
                    raise RuntimeError(
                        "Calendar replacement state could not be synchronized."
                    )
            row = (
                connection.execute(_INSERT_PROPOSAL, params)
                .mappings()
                .first()
            )
            if row is None:
                if supersedes_proposal_id is not None:
                    raise RuntimeError(
                        "Replacement Calendar approval could not be stored."
                    )
                return None
            if calendar_operation is not None:
                connection.execute(
                    _INSERT_CALENDAR_OPERATION,
                    calendar_operation,
                )
            return self._proposal_row(row, account_email)

    def create_gmail_draft_proposal(
        self,
        *,
        proposal_id: UUID,
        idempotency_key: UUID,
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
        try:
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
                    row = self._proposal_row(existing)
                    transaction_outcome = "reused"
                else:
                    row = (
                        connection.execute(
                            _INSERT_GMAIL_DRAFT_PROPOSAL,
                            params,
                        )
                        .mappings()
                        .first()
                    )
                    transaction_outcome = "inserted"
        except Exception as exc:
            logger.error(
                "gmail_draft_approval_database_transaction_failed "
                "| request_id=%s | user_id=%s | approval_id=%s "
                "| idempotency_key=%s | transaction_outcome=unknown "
                "| http_status=503 | diagnostics=%s",
                request_id(),
                user_id,
                proposal_id,
                idempotency_key,
                json.dumps(
                    _database_exception_diagnostics(exc),
                    sort_keys=True,
                ),
            )
            raise

        logger.info(
            "gmail_draft_approval_database_transaction_completed "
            "| request_id=%s | user_id=%s | approval_id=%s "
            "| transaction_outcome=committed | result=%s",
            request_id(),
            user_id,
            proposal_id,
            transaction_outcome if row is not None else "no_proposal_returned",
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
            if (
                row is not None
                and row["proposal"]["action_type"].startswith("calendar.")
                and (
                    (
                        expected_status == "pending"
                        and new_status in {
                            "approved",
                            "rejected",
                            "expired",
                        }
                    )
                    or (
                        expected_status == "approved"
                        and new_status == "expired"
                    )
                )
            ):
                synchronized = connection.execute(
                    _SYNC_CALENDAR_APPROVAL_STATE,
                    {
                        "user_id": str(user_id),
                        "proposal_id": str(proposal_id),
                        "expected_state": expected_status,
                        "new_state": new_status,
                    },
                )
                if synchronized.rowcount != 1:
                    raise RuntimeError(
                        "Calendar approval state could not be synchronized."
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
            "operation_id",
        ):
            if proposal.get(field) is not None:
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


_GET_CALENDAR_OPERATION = text("""
    SELECT *
    FROM public.pa_calendar_action_operations
    WHERE user_id = CAST(:user_id AS uuid)
      AND proposal_id = CAST(:proposal_id AS uuid)
    LIMIT 1
""")

_GET_CALENDAR_OPERATION_BY_KEY = text("""
    SELECT *
    FROM public.pa_calendar_action_operations
    WHERE user_id = CAST(:user_id AS uuid)
      AND idempotency_key = CAST(:idempotency_key AS uuid)
    LIMIT 1
""")

_BIND_CALENDAR_IDEMPOTENCY_KEY = text("""
    UPDATE public.pa_calendar_action_operations
    SET idempotency_key = CAST(:idempotency_key AS uuid),
        updated_at = clock_timestamp()
    WHERE user_id = CAST(:user_id AS uuid)
      AND proposal_id = CAST(:proposal_id AS uuid)
      AND idempotency_key IS NULL
    RETURNING *
""")

_TRANSITION_CALENDAR_OPERATION = text("""
    UPDATE public.pa_calendar_action_operations
    SET execution_state = CAST(:new_state AS text),
        attempt_count = attempt_count
            + CASE WHEN :new_state = 'executing' THEN 1 ELSE 0 END,
        execution_started_at = CASE
            WHEN :new_state = 'executing' THEN clock_timestamp()
            ELSE execution_started_at
        END,
        execution_completed_at = CASE
            WHEN :new_state IN (
                'completed', 'failed', 'execution_uncertain'
            ) THEN clock_timestamp()
            WHEN :new_state IN ('pending', 'approved', 'executing')
                THEN NULL
            ELSE execution_completed_at
        END,
        provider_result_classification = COALESCE(
            CAST(:provider_result_classification AS text),
            provider_result_classification
        ),
        reconciliation_status = COALESCE(
            CAST(:reconciliation_status AS text),
            reconciliation_status
        ),
        reconciled_at = CASE
            WHEN :reconciliation_status IN ('confirmed', 'not_executed')
                THEN clock_timestamp()
            ELSE reconciled_at
        END,
        updated_at = clock_timestamp()
    WHERE user_id = CAST(:user_id AS uuid)
      AND proposal_id = CAST(:proposal_id AS uuid)
      AND execution_state = CAST(:expected_state AS text)
    RETURNING *
""")


class CalendarActionOperationRepository:
    """Durable, owner-scoped operation records for future Calendar writes."""

    ALLOWED_TRANSITIONS = {
        "pending": {"approved", "rejected", "cancelled", "expired"},
        "approved": {"executing", "cancelled", "expired"},
        "executing": {"completed", "failed", "execution_uncertain"},
        "execution_uncertain": {"completed", "failed"},
    }
    PROVIDER_RESULT_CLASSIFICATIONS = frozenset({
        "accepted",
        "rejected",
        "not_transmitted",
        "outcome_unknown",
        "confirmed",
    })
    RECONCILIATION_STATUSES = frozenset({
        "not_required",
        "pending",
        "confirmed",
        "not_executed",
        "not_found",
        "conflict",
        "manual_review",
    })

    def __init__(self, database_engine: Engine | None = None):
        self.engine = database_engine or engine

    def get_for_user(
        self,
        user_id: UUID,
        proposal_id: UUID,
    ) -> dict[str, Any] | None:
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    _GET_CALENDAR_OPERATION,
                    {
                        "user_id": str(user_id),
                        "proposal_id": str(proposal_id),
                    },
                )
                .mappings()
                .first()
            )
        return dict(row) if row is not None else None

    def get_by_idempotency_key(
        self,
        user_id: UUID,
        idempotency_key: UUID,
    ) -> dict[str, Any] | None:
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    _GET_CALENDAR_OPERATION_BY_KEY,
                    {
                        "user_id": str(user_id),
                        "idempotency_key": str(idempotency_key),
                    },
                )
                .mappings()
                .first()
            )
        return dict(row) if row is not None else None

    def claim_execution(
        self,
        *,
        user_id: UUID,
        proposal_id: UUID,
    ) -> dict[str, Any] | None:
        """Atomically verify and claim an approved Calendar proposal."""
        with self.engine.begin() as connection:
            row = (
                connection.execute(
                    _LOCK_CALENDAR_APPROVAL_FOR_EXECUTION,
                    {
                        "user_id": str(user_id),
                        "proposal_id": str(proposal_id),
                    },
                )
                .mappings()
                .first()
            )
            if row is None:
                return None

            proposal_data = ActionApprovalRepository._proposal_row(row)
            proposal = ActionApprovalProposal.model_validate(proposal_data)
            identity = verify_calendar_proposal_integrity(proposal, user_id)
            if (
                UUID(str(row["operation_id"])) != identity.operation_id
                or UUID(str(row["user_id"])) != user_id
                or UUID(str(row["connection_id"])) != identity.connection_id
                or row["action_type"] != identity.action_type
                or row["calendar_id"] != identity.calendar_id
                or row["event_id"] != identity.event_id
                or row["payload_fingerprint"] != identity.payload_fingerprint
                or row["execution_state"] != proposal_data["status"]
                or proposal_data["status"] != "approved"
            ):
                raise CalendarOperationConflictError(
                    "Calendar approval integrity verification failed."
                )

            params = {
                "user_id": str(user_id),
                "proposal_id": str(proposal_id),
            }
            if proposal_data["expires_at"] <= row["database_now"]:
                expired_proposal = connection.execute(
                    _EXPIRE_CALENDAR_APPROVAL_CLAIM,
                    params,
                ).first()
                expired_operation = connection.execute(
                    _EXPIRE_CALENDAR_OPERATION_CLAIM,
                    params,
                ).first()
                if expired_proposal is None or expired_operation is None:
                    raise RuntimeError(
                        "Calendar approval expiration could not be synchronized."
                    )
                return None

            claimed_proposal = (
                connection.execute(
                    _CLAIM_CALENDAR_APPROVAL,
                    params,
                )
                .mappings()
                .first()
            )
            if claimed_proposal is None:
                return None
            claimed_operation = connection.execute(
                _CLAIM_CALENDAR_OPERATION,
                params,
            ).first()
            if claimed_operation is None:
                raise RuntimeError(
                    "Calendar operation claim could not be synchronized."
                )
            return ActionApprovalRepository._proposal_row(claimed_proposal)

    def finish_execution(
        self,
        *,
        user_id: UUID,
        proposal_id: UUID,
        expected_state: str,
        new_state: str,
        provider_result_classification: str,
        reconciliation_status: str,
        safe_result: dict[str, Any],
    ) -> dict[str, Any] | None:
        if expected_state not in {"executing", "execution_uncertain"}:
            raise ValueError("Invalid Calendar execution completion state.")
        if new_state not in {"completed", "failed", "execution_uncertain"}:
            raise ValueError("Invalid Calendar execution completion state.")
        if provider_result_classification not in self.PROVIDER_RESULT_CLASSIFICATIONS:
            raise ValueError("Invalid Calendar provider result classification.")
        if reconciliation_status not in self.RECONCILIATION_STATUSES:
            raise ValueError("Invalid Calendar reconciliation status.")
        if new_state == "completed" and reconciliation_status != "confirmed":
            raise ValueError(
                "Calendar completion requires confirmed provider read-back."
            )
        if expected_state == "execution_uncertain" and (
            (new_state == "completed" and reconciliation_status != "confirmed")
            or (
                new_state == "failed"
                and reconciliation_status != "not_executed"
            )
        ):
            raise ValueError(
                "Uncertain Calendar operations require reconciliation."
            )
        if provider_result_classification in {
            "outcome_unknown",
            "accepted",
        } and new_state == "failed":
            raise ValueError(
                "Uncertain Calendar results cannot be marked failed."
            )

        approval_patch = {
            "status": new_state,
            "result": safe_result,
        }
        with self.engine.begin() as connection:
            params = {
                "user_id": str(user_id),
                "proposal_id": str(proposal_id),
                "expected_state": expected_state,
            }
            locked = (
                connection.execute(
                    _LOCK_CALENDAR_EXECUTION_STATE,
                    params,
                )
                .mappings()
                .first()
            )
            if (
                locked is None
                or locked["execution_state"] != expected_state
                or locked["proposal"].get("status") != expected_state
            ):
                return None

            updated_approval = (
                connection.execute(
                    _UPDATE_CALENDAR_APPROVAL_RESULT,
                    {
                        "proposal_id": str(proposal_id),
                        "expected_state": expected_state,
                        "approval_patch": json.dumps(
                            approval_patch,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    },
                )
                .mappings()
                .first()
            )
            updated_operation = connection.execute(
                _UPDATE_CALENDAR_OPERATION_RESULT,
                {
                    "user_id": str(user_id),
                    "proposal_id": str(proposal_id),
                    "expected_state": expected_state,
                    "new_state": new_state,
                    "provider_result_classification": (
                        provider_result_classification
                    ),
                    "reconciliation_status": reconciliation_status,
                },
            ).first()
            if updated_approval is None or updated_operation is None:
                raise RuntimeError(
                    "Calendar execution result could not be synchronized."
                )
            row = {
                "message_id": locked["message_id"],
                "proposal": updated_approval["proposal"],
                "account_email": None,
            }
        return ActionApprovalRepository._proposal_row(row)

    def update_uncertain_reconciliation(
        self,
        *,
        user_id: UUID,
        proposal_id: UUID,
        reconciliation_status: str,
    ) -> bool:
        if reconciliation_status not in {"pending", "manual_review"}:
            raise ValueError("Invalid uncertain reconciliation status.")
        with self.engine.begin() as connection:
            row = connection.execute(
                _UPDATE_UNCERTAIN_RECONCILIATION,
                {
                    "user_id": str(user_id),
                    "proposal_id": str(proposal_id),
                    "reconciliation_status": reconciliation_status,
                },
            ).first()
        return row is not None

    def mark_stale_execution_uncertain(
        self,
        *,
        user_id: UUID,
        proposal_id: UUID,
        stale_after_seconds: int,
    ) -> dict[str, Any] | None:
        if stale_after_seconds < 1:
            raise ValueError("Stale execution threshold must be positive.")
        params = {
            "user_id": str(user_id),
            "proposal_id": str(proposal_id),
        }
        with self.engine.begin() as connection:
            row = (
                connection.execute(
                    _STALE_CALENDAR_EXECUTION_CHECK,
                    params,
                )
                .mappings()
                .first()
            )
            if (
                row is None
                or row["execution_state"] != "executing"
                or row["proposal"].get("status") != "executing"
                or row["execution_started_at"] is None
                or (
                    row["database_now"] - row["execution_started_at"]
                ).total_seconds() < stale_after_seconds
            ):
                return None

            approval = (
                connection.execute(
                    _MARK_STALE_CALENDAR_APPROVAL_UNCERTAIN,
                    params,
                )
                .mappings()
                .first()
            )
            operation = connection.execute(
                _MARK_STALE_CALENDAR_OPERATION_UNCERTAIN,
                params,
            ).first()
            if approval is None or operation is None:
                raise RuntimeError(
                    "Stale Calendar execution recovery could not be synchronized."
                )
            return ActionApprovalRepository._proposal_row(approval)

    def bind_idempotency_key(
        self,
        *,
        user_id: UUID,
        proposal_id: UUID,
        idempotency_key: UUID,
        payload_fingerprint: str,
    ) -> dict[str, Any]:
        with self.engine.begin() as connection:
            connection.execute(
                _LOCK_PROPOSAL_IDEMPOTENCY,
                {
                    "lock_key": (
                        f"calendar:{user_id}:{idempotency_key}"
                    ),
                },
            )
            existing = (
                connection.execute(
                    _GET_CALENDAR_OPERATION_BY_KEY,
                    {
                        "user_id": str(user_id),
                        "idempotency_key": str(idempotency_key),
                    },
                )
                .mappings()
                .first()
            )
            if existing is not None:
                validate_idempotency_fingerprint_reuse(
                    existing["payload_fingerprint"],
                    payload_fingerprint,
                )
                return dict(existing)

            row = (
                connection.execute(
                    _BIND_CALENDAR_IDEMPOTENCY_KEY,
                    {
                        "user_id": str(user_id),
                        "proposal_id": str(proposal_id),
                        "idempotency_key": str(idempotency_key),
                    },
                )
                .mappings()
                .first()
            )
            if row is None:
                raise LookupError(
                    "Calendar operation was not found or already has an idempotency key."
                )
            validate_idempotency_fingerprint_reuse(
                row["payload_fingerprint"],
                payload_fingerprint,
            )
            return dict(row)

    def transition(
        self,
        *,
        user_id: UUID,
        proposal_id: UUID,
        expected_state: str,
        new_state: str,
        provider_result_classification: str | None = None,
        reconciliation_status: str | None = None,
    ) -> dict[str, Any] | None:
        allowed = self.ALLOWED_TRANSITIONS.get(expected_state, set())
        if new_state not in allowed:
            raise ValueError("Invalid Calendar operation state transition.")
        if (
            provider_result_classification is not None
            and provider_result_classification
            not in self.PROVIDER_RESULT_CLASSIFICATIONS
        ):
            raise ValueError(
                "Invalid Calendar provider result classification."
            )
        if (
            reconciliation_status is not None
            and reconciliation_status not in self.RECONCILIATION_STATUSES
        ):
            raise ValueError("Invalid Calendar reconciliation status.")
        if expected_state == "execution_uncertain":
            required_reconciliation = {
                "completed": "confirmed",
                "failed": "not_executed",
            }[new_state]
            if reconciliation_status != required_reconciliation:
                raise ValueError(
                    "Uncertain Calendar operations require positive reconciliation."
                )
        with self.engine.begin() as connection:
            row = (
                connection.execute(
                    _TRANSITION_CALENDAR_OPERATION,
                    {
                        "user_id": str(user_id),
                        "proposal_id": str(proposal_id),
                        "expected_state": expected_state,
                        "new_state": new_state,
                        "provider_result_classification": (
                            provider_result_classification
                        ),
                        "reconciliation_status": reconciliation_status,
                    },
                )
                .mappings()
                .first()
            )
        return dict(row) if row is not None else None
