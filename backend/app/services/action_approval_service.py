from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy.orm import Session

from app.repositories.action_approval_repository import (
    ActionApprovalRepository,
    CalendarActionOperationRepository,
)
from app.repositories.oauth_credential_repository import (
    OAuthCredentialRepository,
)
from app.repositories.user_connection_repository import (
    UserConnectionRepository,
)
from app.schemas.action_approval import (
    ActionApprovalProposal,
    ApprovalProposalCreate,
    validate_safe_result,
)
from app.schemas.calendar_approval import CalendarCreateEventParameters
from app.services.calendar_action_identity import (
    CalendarOperationConflictError,
    build_calendar_operation_identity,
    verify_calendar_proposal_integrity,
)


ACTION_REQUIRED_SCOPES = {
    "gmail.send": "https://www.googleapis.com/auth/gmail.send",
    "gmail.send_draft": "https://www.googleapis.com/auth/gmail.send",
    "gmail.reply": "https://www.googleapis.com/auth/gmail.send",
    "gmail.reply_all": "https://www.googleapis.com/auth/gmail.send",
    "gmail.forward": "https://www.googleapis.com/auth/gmail.send",
    "gmail.archive": "https://www.googleapis.com/auth/gmail.modify",
    "gmail.trash": "https://www.googleapis.com/auth/gmail.modify",
    "gmail.save_draft": "https://www.googleapis.com/auth/gmail.modify",
    "drive.create_file": "https://www.googleapis.com/auth/drive.file",
    "drive.update_file": "https://www.googleapis.com/auth/drive.file",
    "drive.delete_file": "https://www.googleapis.com/auth/drive.file",
    "calendar.create_event": (
        "https://www.googleapis.com/auth/calendar.events"
    ),
    "calendar.update_event": (
        "https://www.googleapis.com/auth/calendar.events"
    ),
    "calendar.delete_event": (
        "https://www.googleapis.com/auth/calendar.events"
    ),
    "contacts.create_contact": "https://www.googleapis.com/auth/contacts",
    "contacts.update_contact": "https://www.googleapis.com/auth/contacts",
    "contacts.delete_contact": "https://www.googleapis.com/auth/contacts",
}


class ApprovalConflictError(Exception):
    """Raised when an approval proposal has already been resolved."""


class ApprovalExpiredError(Exception):
    """Raised when an approval proposal has expired."""


class ActionApprovalService:
    PROPOSAL_TTL_MINUTES = 15
    GMAIL_SEND_STALE_AFTER_SECONDS = 120
    RESULT_STATES = {"completed", "failed", "execution_uncertain"}

    def __init__(
        self,
        db: Session,
        repository: ActionApprovalRepository | None = None,
    ):
        self.db = db
        self.repository = repository or ActionApprovalRepository()
        self.calendar_operation_repository = CalendarActionOperationRepository(
            database_engine=self.repository.engine,
        )
        self.connection_repository = UserConnectionRepository(db)
        self.credential_repository = OAuthCredentialRepository(db)

    def _validate_connection(
        self,
        user_id: UUID,
        connection_id: UUID,
        action_type: str,
    ) -> Any:
        connection = self.connection_repository.get_active_google_connection(
            user_id=user_id,
            connection_id=connection_id,
        )
        if connection is None:
            raise LookupError("Connected Google account was not found.")

        credential = self.credential_repository.get_by_connection(
            user_id=user_id,
            connection_id=connection_id,
        )
        if credential is None:
            raise LookupError("Google OAuth credentials were not found.")

        required_scope = ACTION_REQUIRED_SCOPES[action_type]
        if required_scope not in set(credential.scopes or []):
            raise PermissionError(
                f"The selected account has not granted the required "
                f"permission: {required_scope}"
            )
        return connection

    def create_proposal(
        self,
        user_id: UUID,
        payload: ApprovalProposalCreate,
        proposal_id: UUID | None = None,
    ) -> ActionApprovalProposal:
        if payload.action_type.startswith("calendar.") and (
            payload.action_type != "calendar.create_event"
        ):
            raise PermissionError(
                "Only Calendar event creation proposals are available."
            )
        if payload.action_type == "calendar.create_event":
            parameters = CalendarCreateEventParameters.model_validate(
                payload.parameters
            )
            payload.parameters = parameters.model_dump(mode="json")
        connection = self._validate_connection(
            user_id=user_id,
            connection_id=payload.connection_id,
            action_type=payload.action_type,
        )
        proposal_id = proposal_id or uuid4()
        calendar_operation = None
        if payload.action_type.startswith("calendar."):
            identity = build_calendar_operation_identity(
                proposal_id=proposal_id,
                user_id=user_id,
                connection_id=payload.connection_id,
                action_type=payload.action_type,
                parameters=payload.parameters,
            )
            calendar_operation = {
                "proposal_id": str(identity.proposal_id),
                "operation_id": str(identity.operation_id),
                "user_id": str(identity.user_id),
                "connection_id": str(identity.connection_id),
                "idempotency_key": None,
                "payload_fingerprint": identity.payload_fingerprint,
                "action_type": identity.action_type,
                "calendar_id": identity.calendar_id,
                "event_id": identity.event_id,
                "integrity_schema_version": (
                    identity.integrity_schema_version
                ),
            }
        row = self.repository.create_proposal(
            proposal_id=proposal_id,
            user_id=user_id,
            conversation_id=payload.conversation_id,
            source_message_id=payload.source_message_id,
            action_type=payload.action_type,
            parameters=payload.parameters,
            connection_id=payload.connection_id,
            account_email=connection.email,
            expiration_minutes=self.PROPOSAL_TTL_MINUTES,
            calendar_operation=calendar_operation,
        )
        if row is None:
            raise LookupError(
                "The conversation or source assistant message was not found."
            )
        return ActionApprovalProposal.model_validate(row)

    def create_gmail_draft_proposal(
        self,
        user_id: UUID,
        connection_id: UUID,
        message_id: str,
        idempotency_key: UUID,
        parameters: dict[str, Any],
    ) -> ActionApprovalProposal:
        connection = self._validate_connection(
            user_id=user_id,
            connection_id=connection_id,
            action_type="gmail.send_draft",
        )
        validated = ApprovalProposalCreate.model_validate({
            "conversation_id": uuid4(),
            "source_message_id": uuid4(),
            "connection_id": connection_id,
            "action_type": "gmail.send_draft",
            "parameters": parameters,
        })
        proposal_id = self._gmail_draft_proposal_id(
            user_id,
            idempotency_key,
        )
        row = self.repository.create_gmail_draft_proposal(
            proposal_id=proposal_id,
            idempotency_key=idempotency_key,
            source_message_id=validated.source_message_id,
            conversation_id=validated.conversation_id,
            user_id=user_id,
            parameters=validated.parameters,
            connection_id=connection_id,
            account_email=connection.email,
            expiration_minutes=self.PROPOSAL_TTL_MINUTES,
        )
        proposal = ActionApprovalProposal.model_validate(row)
        self.validate_gmail_draft_request_match(
            proposal=proposal,
            connection_id=connection_id,
            message_id=message_id,
            parameters=validated.parameters,
        )
        return proposal

    @staticmethod
    def _gmail_draft_proposal_id(
        user_id: UUID,
        idempotency_key: UUID,
    ) -> UUID:
        return uuid5(
            NAMESPACE_URL,
            f"gmail-draft-send:{user_id}:{idempotency_key}",
        )

    def get_gmail_draft_proposal(
        self,
        user_id: UUID,
        idempotency_key: UUID,
    ) -> ActionApprovalProposal | None:
        row = self.repository.get_gmail_draft_proposal(
            user_id=user_id,
            proposal_id=self._gmail_draft_proposal_id(
                user_id,
                idempotency_key,
            ),
        )
        return ActionApprovalProposal.model_validate(row) if row else None

    @staticmethod
    def validate_gmail_draft_request_match(
        *,
        proposal: ActionApprovalProposal,
        connection_id: UUID,
        message_id: str,
        parameters: dict[str, Any],
    ) -> None:
        existing_parameters = proposal.parameters
        matching = (
            proposal.action_type == "gmail.send_draft"
            and proposal.connection_id == connection_id
            and existing_parameters.get("message_id") == message_id
            and existing_parameters.get("to") == parameters.get("to")
            and existing_parameters.get("cc", []) == parameters.get("cc", [])
            and existing_parameters.get("bcc", []) == parameters.get("bcc", [])
            and existing_parameters.get("subject") == parameters.get("subject")
            and existing_parameters.get("body") == parameters.get("body")
        )
        if not matching:
            raise ApprovalConflictError(
                "This idempotency key already belongs to a different Gmail draft approval."
            )

    def list_pending(
        self,
        user_id: UUID,
        conversation_id: UUID | None = None,
    ) -> list[ActionApprovalProposal]:
        rows = self.repository.list_pending(
            user_id=user_id,
            conversation_id=conversation_id,
        )
        return [
            ActionApprovalProposal.model_validate(row)
            for row in rows
        ]

    def get_proposal(
        self,
        user_id: UUID,
        proposal_id: UUID,
    ) -> ActionApprovalProposal:
        self.repository.recover_stale_gmail_send(
            user_id=user_id,
            proposal_id=proposal_id,
            stale_after_seconds=self.GMAIL_SEND_STALE_AFTER_SECONDS,
        )
        row = self.repository.get_proposal(user_id, proposal_id)
        if row is None:
            raise LookupError("Approval proposal was not found.")
        proposal = ActionApprovalProposal.model_validate(row)
        if proposal.action_type.startswith("calendar."):
            self._verify_calendar_operation(proposal, user_id)
        return proposal

    def _verify_calendar_operation(
        self,
        proposal: ActionApprovalProposal,
        user_id: UUID,
    ) -> None:
        try:
            identity = verify_calendar_proposal_integrity(
                proposal,
                user_id,
            )
            operation = self.calendar_operation_repository.get_for_user(
                user_id,
                proposal.proposal_id,
            )
            matches = (
                operation is not None
                and UUID(str(operation["operation_id"]))
                == identity.operation_id
                and UUID(str(operation["user_id"])) == user_id
                and UUID(str(operation["connection_id"]))
                == identity.connection_id
                and operation["action_type"] == identity.action_type
                and operation["calendar_id"] == identity.calendar_id
                and operation["event_id"] == identity.event_id
                and operation["payload_fingerprint"]
                == identity.payload_fingerprint
                and operation["execution_state"] == proposal.status
            )
        except (
            CalendarOperationConflictError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            raise ApprovalConflictError(
                "Calendar approval integrity verification failed."
            ) from exc
        if not matches:
            raise ApprovalConflictError(
                "Calendar approval integrity verification failed."
            )

    def approve(
        self,
        user_id: UUID,
        proposal_id: UUID,
    ) -> ActionApprovalProposal:
        proposal = self.get_proposal(user_id, proposal_id)
        if proposal.status != "pending":
            raise ApprovalConflictError(
                "This proposal has already been resolved."
            )
        self._validate_connection(
            user_id=user_id,
            connection_id=proposal.connection_id,
            action_type=proposal.action_type,
        )
        transitioned = self.repository.transition(
            user_id=user_id,
            proposal_id=proposal_id,
            expected_status="pending",
            new_status="approved",
            expiry_mode="valid",
            timestamp_field="approved_at",
        )
        if transitioned is not None:
            return self.get_proposal(user_id, proposal_id)
        self._expire_if_due(user_id, proposal_id, "pending")
        current = self.get_proposal(user_id, proposal_id)
        self._raise_resolution_error(current)
        raise ApprovalConflictError(
            "This proposal changed while it was being approved."
        )

    def reject(
        self,
        user_id: UUID,
        proposal_id: UUID,
    ) -> ActionApprovalProposal:
        transitioned = self.repository.transition(
            user_id=user_id,
            proposal_id=proposal_id,
            expected_status="pending",
            new_status="rejected",
            expiry_mode="valid",
            timestamp_field="completed_at",
        )
        if transitioned is not None:
            return self.get_proposal(user_id, proposal_id)
        self._expire_if_due(user_id, proposal_id, "pending")
        current = self.get_proposal(user_id, proposal_id)
        self._raise_resolution_error(current)
        raise ApprovalConflictError(
            "This proposal changed while it was being rejected."
        )

    def claim_calendar_execution(
        self,
        user_id: UUID,
        proposal_id: UUID,
    ) -> ActionApprovalProposal:
        proposal = self.get_proposal(user_id, proposal_id)
        if proposal.action_type != "calendar.create_event":
            raise PermissionError(
                "Only Calendar event creation can be executed."
            )
        if proposal.status != "approved":
            raise ApprovalConflictError(
                "Only an approved Calendar proposal can be executed."
            )
        self._validate_connection(
            user_id=user_id,
            connection_id=proposal.connection_id,
            action_type=proposal.action_type,
        )
        try:
            claimed = self.calendar_operation_repository.claim_execution(
                user_id=user_id,
                proposal_id=proposal_id,
            )
        except CalendarOperationConflictError as exc:
            raise ApprovalConflictError(
                "Calendar approval integrity verification failed."
            ) from exc
        if claimed is None:
            current = self.get_proposal(user_id, proposal_id)
            if current.status == "expired":
                raise ApprovalExpiredError(
                    "This approval proposal has expired."
                )
            raise ApprovalConflictError(
                "This proposal is already being executed or was resolved."
            )
        return ActionApprovalProposal.model_validate(claimed)

    def finish_calendar_execution(
        self,
        user_id: UUID,
        proposal_id: UUID,
        *,
        expected_state: str,
        state: str,
        provider_result_classification: str,
        reconciliation_status: str,
        safe_result: dict[str, Any],
    ) -> ActionApprovalProposal:
        result = validate_safe_result(safe_result)
        transitioned = self.calendar_operation_repository.finish_execution(
            user_id=user_id,
            proposal_id=proposal_id,
            expected_state=expected_state,
            new_state=state,
            provider_result_classification=provider_result_classification,
            reconciliation_status=reconciliation_status,
            safe_result=result,
        )
        if transitioned is None:
            raise ApprovalConflictError(
                "The Calendar proposal is not in the expected execution state."
            )
        return ActionApprovalProposal.model_validate(transitioned)

    def claim_execution(
        self,
        user_id: UUID,
        proposal_id: UUID,
    ) -> ActionApprovalProposal:
        """Atomically claim an approved Gmail-send proposal."""
        proposal = self.get_proposal(user_id, proposal_id)
        if proposal.action_type not in {"gmail.send", "gmail.send_draft"}:
            raise PermissionError(
                "Only Gmail send actions can be executed in this phase."
            )
        if proposal.status != "approved":
            if proposal.status == "pending":
                self._expire_if_due(user_id, proposal_id, "pending")
                proposal = self.get_proposal(user_id, proposal_id)
                if proposal.status == "expired":
                    raise ApprovalExpiredError(
                        "This approval proposal has expired."
                    )
            raise ApprovalConflictError(
                "Only an approved proposal can be executed."
            )
        self._validate_connection(
            user_id=user_id,
            connection_id=proposal.connection_id,
            action_type=proposal.action_type,
        )
        transitioned = self.repository.transition(
            user_id=user_id,
            proposal_id=proposal_id,
            expected_status="approved",
            new_status="executing",
            expiry_mode="valid",
            timestamp_field="execution_started_at",
        )
        if transitioned is not None:
            return self.get_proposal(user_id, proposal_id)
        self._expire_if_due(user_id, proposal_id, "approved")
        current = self.get_proposal(user_id, proposal_id)
        self._raise_resolution_error(current)
        raise ApprovalConflictError(
            "This proposal is already being executed or was resolved."
        )

    def finish_execution(
        self,
        user_id: UUID,
        proposal_id: UUID,
        state: str,
        safe_result: dict[str, Any] | None = None,
    ) -> ActionApprovalProposal:
        if state not in self.RESULT_STATES:
            raise ValueError("Invalid execution result state.")
        result = (
            validate_safe_result(safe_result)
            if safe_result is not None
            else None
        )
        transitioned = self.repository.transition(
            user_id=user_id,
            proposal_id=proposal_id,
            expected_status="executing",
            new_status=state,
            timestamp_field="completed_at",
            result=result,
        )
        if transitioned is None:
            raise ApprovalConflictError(
                "The proposal is not owned by this user or is not executing."
            )
        return self.get_proposal(user_id, proposal_id)

    def finish_reconciled_execution(
        self,
        user_id: UUID,
        proposal_id: UUID,
        safe_result: dict[str, Any],
    ) -> ActionApprovalProposal:
        result = validate_safe_result(safe_result)
        transitioned = self.repository.transition(
            user_id=user_id,
            proposal_id=proposal_id,
            expected_status="execution_uncertain",
            new_status="completed",
            timestamp_field="completed_at",
            result=result,
        )
        if transitioned is None:
            raise ApprovalConflictError(
                "The uncertain proposal is not owned by this user."
            )
        return self.get_proposal(user_id, proposal_id)

    def _expire_if_due(
        self,
        user_id: UUID,
        proposal_id: UUID,
        expected_status: str,
    ) -> None:
        self.repository.transition(
            user_id=user_id,
            proposal_id=proposal_id,
            expected_status=expected_status,
            new_status="expired",
            expiry_mode="expired",
            timestamp_field="completed_at",
        )

    @staticmethod
    def _raise_resolution_error(
        proposal: ActionApprovalProposal,
    ) -> None:
        if proposal.status == "expired":
            raise ApprovalExpiredError("This approval proposal has expired.")
        if proposal.status != "pending":
            raise ApprovalConflictError(
                "This proposal has already been resolved."
            )
