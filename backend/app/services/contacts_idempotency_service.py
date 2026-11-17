from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.schemas.action_approval import ActionApprovalProposal
from app.services.action_approval_service import ActionApprovalService
from app.services.contacts_idempotency import (
    ContactsCreateIdempotencyContext,
    contacts_create_replay_until,
)


class ContactsIdempotencyConflictError(Exception):
    """Same idempotency key scope reused with a different payload."""


@dataclass(frozen=True)
class ContactsIdempotentCreateResult:
    proposal: ActionApprovalProposal
    replayed: bool


_INSERT_IDEMPOTENCY = text("""
    INSERT INTO public.pa_contacts_action_idempotency (
        tenant_id,
        user_id,
        connection_id,
        action_type,
        idempotency_key,
        payload_fingerprint,
        normalization_version,
        proposal_id,
        proposal_storage,
        request_hash_alg,
        replay_until
    )
    VALUES (
        CAST(:tenant_id AS uuid),
        CAST(:user_id AS uuid),
        CAST(:connection_id AS uuid),
        CAST(:action_type AS text),
        CAST(:idempotency_key AS text),
        CAST(:payload_fingerprint AS text),
        CAST(:normalization_version AS smallint),
        CAST(:proposal_id AS uuid),
        CAST(:proposal_storage AS text),
        CAST(:request_hash_alg AS text),
        CAST(:replay_until AS timestamptz)
    )
    ON CONFLICT (
        tenant_id, user_id, connection_id, action_type, idempotency_key
    ) DO NOTHING
    RETURNING id, proposal_id, payload_fingerprint, normalization_version
""")

_SELECT_IDEMPOTENCY_FOR_UPDATE = text("""
    SELECT
        id,
        proposal_id,
        payload_fingerprint,
        normalization_version
    FROM public.pa_contacts_action_idempotency
    WHERE tenant_id = CAST(:tenant_id AS uuid)
      AND user_id = CAST(:user_id AS uuid)
      AND connection_id = CAST(:connection_id AS uuid)
      AND action_type = CAST(:action_type AS text)
      AND idempotency_key = CAST(:idempotency_key AS text)
    FOR UPDATE
""")


class ContactsIdempotencyService:
    def __init__(self, db: Session):
        self.db = db
        self.approval_service = ActionApprovalService(db)

    @staticmethod
    def _ensure_create_enabled(action_type: str) -> None:
        if action_type != "contacts.create_contact":
            raise ValueError("Idempotency is only supported for contacts.create_contact.")
        if not settings.CONTACTS_CREATE_ENABLED:
            raise PermissionError("Contacts create is currently disabled.")

    def _load_existing_proposal(self, *, user_id: UUID, proposal_id: UUID) -> ActionApprovalProposal:
        proposal = self.approval_service.get_proposal(user_id, proposal_id)
        if proposal.action_type != "contacts.create_contact":
            raise LookupError("The existing Contacts create proposal could not be found.")
        return proposal

    def _handle_existing(self, connection, *, user_id: UUID, context: ContactsCreateIdempotencyContext) -> ContactsIdempotentCreateResult:
        row = (
            connection.execute(
                _SELECT_IDEMPOTENCY_FOR_UPDATE,
                {
                    "tenant_id": str(context.tenant_id),
                    "user_id": str(context.user_id),
                    "connection_id": str(context.connection_id),
                    "action_type": context.action_type,
                    "idempotency_key": context.idempotency_key,
                },
            )
            .mappings()
            .first()
        )
        if row is None:
            raise RuntimeError("Contacts idempotency record could not be resolved.")
        if (
            str(row["payload_fingerprint"]) != context.payload_fingerprint
            or int(row["normalization_version"]) != int(context.normalization_version)
        ):
            raise ContactsIdempotencyConflictError(
                "This Idempotency-Key was already used with a different Contacts create request."
            )
        proposal = self._load_existing_proposal(user_id=user_id, proposal_id=UUID(str(row["proposal_id"])))
        # Replay must never trigger execution or mutate lifecycle state.
        return ContactsIdempotentCreateResult(proposal=proposal, replayed=True)

    def create_contacts_create_proposal(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        context: ContactsCreateIdempotencyContext,
    ) -> ContactsIdempotentCreateResult:
        self._ensure_create_enabled(context.action_type)
        if context.user_id != user_id or context.connection_id != connection_id:
            raise ValueError("Idempotency context does not match the resolved request.")

        proposal_id = uuid4()
        source_message_id = uuid4()
        conversation_id = uuid4()
        replay_until = contacts_create_replay_until(datetime.now(timezone.utc))

        bind = self.db.get_bind()
        # Use a single DB transaction/connection for ledger + proposal.
        with bind.begin() as connection:
            inserted = (
                connection.execute(
                    _INSERT_IDEMPOTENCY,
                    {
                        "tenant_id": str(context.tenant_id),
                        "user_id": str(context.user_id),
                        "connection_id": str(context.connection_id),
                        "action_type": context.action_type,
                        "idempotency_key": context.idempotency_key,
                        "payload_fingerprint": context.payload_fingerprint,
                        "normalization_version": context.normalization_version,
                        "proposal_id": str(proposal_id),
                        "proposal_storage": "conversation_message",
                        "request_hash_alg": context.request_hash_alg,
                        "replay_until": replay_until,
                    },
                )
                .mappings()
                .first()
            )
            if inserted is not None:
                proposal = self.approval_service.create_standalone_contacts_proposal(
                    user_id=user_id,
                    connection_id=connection_id,
                    action_type=context.action_type,
                    parameters=context.normalized_parameters,
                    proposal_id=proposal_id,
                    source_message_id=source_message_id,
                    conversation_id=conversation_id,
                    db_connection=connection,
                )
                return ContactsIdempotentCreateResult(proposal=proposal, replayed=False)

            # Concurrent insert conflict (or existing key): resolve under same tx.
            return self._handle_existing(connection, user_id=user_id, context=context)
