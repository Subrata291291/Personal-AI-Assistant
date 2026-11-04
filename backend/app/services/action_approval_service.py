from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.repositories.action_approval_repository import (
    ActionApprovalRepository,
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


ACTION_REQUIRED_SCOPES = {
    "gmail.send": "https://www.googleapis.com/auth/gmail.send",
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
    ) -> ActionApprovalProposal:
        connection = self._validate_connection(
            user_id=user_id,
            connection_id=payload.connection_id,
            action_type=payload.action_type,
        )
        row = self.repository.create_proposal(
            proposal_id=uuid4(),
            user_id=user_id,
            conversation_id=payload.conversation_id,
            source_message_id=payload.source_message_id,
            action_type=payload.action_type,
            parameters=payload.parameters,
            connection_id=payload.connection_id,
            account_email=connection.email,
            expiration_minutes=self.PROPOSAL_TTL_MINUTES,
        )
        if row is None:
            raise LookupError(
                "The conversation or source assistant message was not found."
            )
        return ActionApprovalProposal.model_validate(row)

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
        return ActionApprovalProposal.model_validate(row)

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

    def claim_execution(
        self,
        user_id: UUID,
        proposal_id: UUID,
    ) -> ActionApprovalProposal:
        """Atomically claim an approved Gmail-send proposal."""
        proposal = self.get_proposal(user_id, proposal_id)
        if proposal.action_type != "gmail.send":
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
