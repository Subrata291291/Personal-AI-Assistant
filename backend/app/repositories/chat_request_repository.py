
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.db.session import engine

class IdempotencyConflictError(Exception):
    """Raised when an idempotency key is reused with a different payload."""
    pass

class ChatRequestRepository:
    """
    Database repository for chat request idempotency.

    Every operation is explicitly scoped by:
    - user_id
    - conversation_id
    - idempotency_key

    The backend uses a trusted database role with BYPASSRLS,
    so explicit ownership scoping is mandatory.
    """

    def create_request(
        self,
        user_id: UUID,
        conversation_id: UUID,
        idempotency_key: UUID,
        request_hash: str,
    ) -> tuple[dict[str, Any], bool]:
        """
        Create a processing request.

        Returns:
            (request_record, created)
        """

        if len(request_hash) != 64:
            raise ValueError("request_hash must be a SHA-256 hex digest.")

        insert_query = text("""
            INSERT INTO public.pa_chat_requests (
                user_id,
                conversation_id,
                idempotency_key,
                request_hash,
                status
            )
            VALUES (
                :user_id,
                :conversation_id,
                :idempotency_key,
                :request_hash,
                'processing'
            )
            ON CONFLICT (
                user_id,
                conversation_id,
                idempotency_key
            )
            DO NOTHING
            RETURNING *
        """)

        existing_query = text("""
            SELECT *
            FROM public.pa_chat_requests
            WHERE user_id = :user_id
              AND conversation_id = :conversation_id
              AND idempotency_key = :idempotency_key
            LIMIT 1
        """)

        parameters = {
            "user_id": user_id,
            "conversation_id": conversation_id,
            "idempotency_key": idempotency_key,
            "request_hash": request_hash,
        }

        with engine.begin() as connection:
            inserted = (
                connection.execute(insert_query, parameters)
                .mappings()
                .first()
            )

            if inserted is not None:
                return dict(inserted), True

            existing = (
                connection.execute(existing_query, parameters)
                .mappings()
                .one_or_none()
            )

            if existing is None:
                raise RuntimeError(
                    "Request conflict occurred, but the existing "
                    "request could not be retrieved."
                )

            existing_record = dict(existing)

            if existing_record.get("request_hash") != request_hash:
                raise IdempotencyConflictError(
                    "This Idempotency-Key has already been used "
                    "with a different request payload."
                )

            return existing_record, False

    def get_request(
        self,
        user_id: UUID,
        conversation_id: UUID,
        idempotency_key: UUID,
    ) -> dict[str, Any] | None:

        query = text("""
            SELECT *
            FROM public.pa_chat_requests
            WHERE user_id = :user_id
              AND conversation_id = :conversation_id
              AND idempotency_key = :idempotency_key
            LIMIT 1
        """)

        parameters = {
            "user_id": user_id,
            "conversation_id": conversation_id,
            "idempotency_key": idempotency_key,
        }

        with engine.connect() as connection:
            result = (
                connection.execute(query, parameters)
                .mappings()
                .one_or_none()
            )

            return dict(result) if result is not None else None

    def claim_request(
        self,
        user_id: UUID,
        conversation_id: UUID,
        idempotency_key: UUID,
        worker_id: str,
        lease_seconds: int = 600,
    ) -> dict[str, Any] | None:
        """
        Claim an eligible processing request.

        An expired lease can only be reclaimed if LLM execution
        has not started.
        """

        if not worker_id or not worker_id.strip():
            raise ValueError("worker_id cannot be empty.")

        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive.")

        query = text("""
            UPDATE public.pa_chat_requests
            SET
                worker_id = :worker_id,
                lease_expires_at =
                    now() + make_interval(secs => :lease_seconds),
                attempt_count = attempt_count + 1,
                updated_at = now()
            WHERE user_id = :user_id
              AND conversation_id = :conversation_id
              AND idempotency_key = :idempotency_key
              AND status = 'processing'
              AND execution_started_at IS NULL
              AND (
                  lease_expires_at IS NULL
                  OR lease_expires_at <= now()
              )
            RETURNING *
        """)

        parameters = {
            "user_id": user_id,
            "conversation_id": conversation_id,
            "idempotency_key": idempotency_key,
            "worker_id": worker_id,
            "lease_seconds": lease_seconds,
        }

        with engine.begin() as connection:
            result = (
                connection.execute(query, parameters)
                .mappings()
                .one_or_none()
            )

            return dict(result) if result is not None else None

    def attach_user_message(
        self,
        user_id: UUID,
        conversation_id: UUID,
        idempotency_key: UUID,
        worker_id: str,
        user_message_id: UUID,
    ) -> dict[str, Any] | None:

        query = text("""
            UPDATE public.pa_chat_requests
            SET
                user_message_id = :user_message_id,
                updated_at = now()
            WHERE user_id = :user_id
              AND conversation_id = :conversation_id
              AND idempotency_key = :idempotency_key
              AND worker_id = :worker_id
              AND status = 'processing'
              AND execution_started_at IS NULL
              AND lease_expires_at > now()
              AND user_message_id IS NULL
            RETURNING *
        """)

        parameters = {
            "user_id": user_id,
            "conversation_id": conversation_id,
            "idempotency_key": idempotency_key,
            "worker_id": worker_id,
            "user_message_id": user_message_id,
        }

        with engine.begin() as connection:
            result = (
                connection.execute(query, parameters)
                .mappings()
                .one_or_none()
            )

            return dict(result) if result is not None else None

    def mark_execution_started(
        self,
        user_id: UUID,
        conversation_id: UUID,
        idempotency_key: UUID,
        worker_id: str,
    ) -> dict[str, Any] | None:
        """
        Mark the point immediately before LLM execution.

        Once marked, the request cannot be automatically
        reclaimed by another worker.
        """

        query = text("""
            UPDATE public.pa_chat_requests
            SET
                execution_started_at = now(),
                updated_at = now()
            WHERE user_id = :user_id
              AND conversation_id = :conversation_id
              AND idempotency_key = :idempotency_key
              AND worker_id = :worker_id
              AND status = 'processing'
              AND execution_started_at IS NULL
              AND user_message_id IS NOT NULL
              AND lease_expires_at > now()
            RETURNING *
        """)

        parameters = {
            "user_id": user_id,
            "conversation_id": conversation_id,
            "idempotency_key": idempotency_key,
            "worker_id": worker_id,
        }

        with engine.begin() as connection:
            result = (
                connection.execute(query, parameters)
                .mappings()
                .one_or_none()
            )

            return dict(result) if result is not None else None

    def update_status(
        self,
        user_id: UUID,
        conversation_id: UUID,
        idempotency_key: UUID,
        worker_id: str,
        status: str,
        user_message_id: UUID | None = None,
        assistant_message_id: UUID | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any] | None:
        """
        Complete or fail a request only when the current
        worker still owns an active lease.
        """

        allowed_statuses = {"completed", "failed"}

        if status not in allowed_statuses:
            raise ValueError(f"Invalid target status: {status}")

        if not worker_id or not worker_id.strip():
            raise ValueError("worker_id cannot be empty.")

        query = text("""
            UPDATE public.pa_chat_requests
            SET
                status = :status,
                user_message_id = COALESCE(
                    :user_message_id,
                    user_message_id
                ),
                assistant_message_id = COALESCE(
                    :assistant_message_id,
                    assistant_message_id
                ),
                error_message = :error_message,
                worker_id = NULL,
                lease_expires_at = NULL,
                updated_at = now()
            WHERE user_id = :user_id
              AND conversation_id = :conversation_id
              AND idempotency_key = :idempotency_key
              AND worker_id = :worker_id
              AND status = 'processing'
              AND lease_expires_at > now()
            RETURNING *
        """)

        parameters = {
            "user_id": user_id,
            "conversation_id": conversation_id,
            "idempotency_key": idempotency_key,
            "worker_id": worker_id,
            "status": status,
            "user_message_id": user_message_id,
            "assistant_message_id": assistant_message_id,
            "error_message": error_message,
        }

        with engine.begin() as connection:
            result = (
                connection.execute(query, parameters)
                .mappings()
                .one_or_none()
            )

            return dict(result) if result is not None else None


    
    def reconcile_completed_request(
        self,
        user_id: UUID,
        conversation_id: UUID,
        idempotency_key: UUID,
        user_message_id: UUID,
        assistant_message_id: UUID,
    ) -> dict[str, Any] | None:
        """
        Recover a request when its assistant message was saved,
        but the original worker crashed before finalizing it.

        Reconciliation is allowed only after the original lease expires.
        No LLM execution is performed here.
        """

        query = text("""
            UPDATE public.pa_chat_requests
            SET
                status = 'completed',
                user_message_id = :user_message_id,
                assistant_message_id = :assistant_message_id,
                error_message = NULL,
                worker_id = NULL,
                lease_expires_at = NULL,
                updated_at = now()
            WHERE user_id = :user_id
              AND conversation_id = :conversation_id
              AND idempotency_key = :idempotency_key
              AND status = 'processing'
              AND execution_started_at IS NOT NULL
              AND lease_expires_at <= now()
              AND user_message_id = :user_message_id
              AND assistant_message_id IS NULL
            RETURNING *
        """)

        parameters = {
            "user_id": user_id,
            "conversation_id": conversation_id,
            "idempotency_key": idempotency_key,
            "user_message_id": user_message_id,
            "assistant_message_id": assistant_message_id,
        }

        with engine.begin() as connection:
            result = (
                connection.execute(query, parameters)
                .mappings()
                .one_or_none()
            )

            return dict(result) if result is not None else None


    
    def renew_lease(
        self,
        user_id: UUID,
        conversation_id: UUID,
        idempotency_key: UUID,
        worker_id: str,
        lease_seconds: int = 600,
    ) -> dict[str, Any] | None:
        """
        Renew an active worker lease.

        Renewal is allowed only when:
        - The request belongs to the supplied user and conversation.
        - The worker ID matches the current owner.
        - The request is still processing.
        - The existing lease has not expired.

        This also supports requests whose LLM execution has started.
        """

        if not worker_id or not worker_id.strip():
            raise ValueError("worker_id cannot be empty.")

        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive.")

        query = text("""
            UPDATE public.pa_chat_requests
            SET
                lease_expires_at =
                    now() + make_interval(secs => :lease_seconds),
                updated_at = now()
            WHERE user_id = :user_id
              AND conversation_id = :conversation_id
              AND idempotency_key = :idempotency_key
              AND worker_id = :worker_id
              AND status = 'processing'
              AND lease_expires_at > now()
            RETURNING *
        """)

        parameters = {
            "user_id": user_id,
            "conversation_id": conversation_id,
            "idempotency_key": idempotency_key,
            "worker_id": worker_id,
            "lease_seconds": lease_seconds,
        }

        with engine.begin() as connection:
            result = (
                connection.execute(query, parameters)
                .mappings()
                .one_or_none()
            )

            return dict(result) if result is not None else None
