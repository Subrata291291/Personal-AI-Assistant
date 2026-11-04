import logging
import re
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.integrations.google.exceptions import GoogleAPIError
from app.schemas.action_approval import (
    ActionApprovalProposal,
    ApprovalProposalCreate,
)
from app.services.action_approval_service import (
    ActionApprovalService,
)
from app.services.gmail_service import GmailService


logger = logging.getLogger(__name__)
_GMAIL_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,255}$")


class ExecutionStatePersistenceError(Exception):
    """Raised when an external result cannot be durably recorded."""


class GmailActionExecutor:
    """Execute only explicitly approved, immutable Gmail-send proposals."""

    def __init__(
        self,
        db: Session,
        approval_service: ActionApprovalService | None = None,
        gmail_service: GmailService | None = None,
    ):
        self.approval_service = approval_service or ActionApprovalService(db)
        self.gmail_service = gmail_service or GmailService(db)

    async def execute(
        self,
        user_id: UUID,
        proposal_id: UUID,
    ) -> ActionApprovalProposal:
        proposal = self.approval_service.get_proposal(
            user_id=user_id,
            proposal_id=proposal_id,
        )
        if proposal.action_type != "gmail.send":
            raise PermissionError(
                "Only Gmail send actions can be executed in this phase."
            )
        self._validate_stored_send(proposal)

        claimed = self.approval_service.claim_execution(
            user_id=user_id,
            proposal_id=proposal_id,
        )
        if claimed.action_type != "gmail.send":
            raise PermissionError(
                "Only Gmail send actions can be executed in this phase."
            )

        try:
            validated = self._validate_stored_send(claimed)
            parameters = validated.parameters
            result = await self.gmail_service.send_message(
                user_id=user_id,
                connection_id=claimed.connection_id,
                to=parameters["to"],
                cc=parameters.get("cc"),
                bcc=parameters.get("bcc"),
                subject=parameters["subject"],
                body=parameters["body"],
            )
        except GoogleAPIError as exc:
            uncertain = exc.status_code == 408 or exc.status_code >= 500
            state = "execution_uncertain" if uncertain else "failed"
            safe_result = {
                "outcome": "unconfirmed" if uncertain else "rejected",
                "provider_status_code": exc.status_code,
            }
            return self._finish_safely(
                user_id,
                proposal_id,
                state,
                safe_result,
            )
        except (PermissionError, LookupError, ValueError) as exc:
            return self._finish_safely(
                user_id,
                proposal_id,
                "failed",
                {"outcome": "rejected", "reason": _safe_reason(exc)},
            )
        except Exception as exc:
            logger.exception(
                "Gmail send execution failed with an unconfirmed outcome."
            )
            return self._finish_safely(
                user_id,
                proposal_id,
                "execution_uncertain",
                {"outcome": "unconfirmed", "reason": _safe_reason(exc)},
            )

        message_id = result.get("id")
        if (
            not isinstance(message_id, str)
            or not _GMAIL_ID_PATTERN.fullmatch(message_id)
        ):
            return self._finish_safely(
                user_id,
                proposal_id,
                "execution_uncertain",
                {"outcome": "unconfirmed", "reason": "missing_gmail_message_id"},
            )

        safe_result = {
            "outcome": "sent",
            "gmail_message_id": message_id,
        }
        thread_id = result.get("thread_id")
        if isinstance(thread_id, str) and _GMAIL_ID_PATTERN.fullmatch(thread_id):
            safe_result["gmail_thread_id"] = thread_id
        label_ids = result.get("label_ids")
        if isinstance(label_ids, list):
            safe_result["label_ids"] = [
                label for label in label_ids[:20]
                if isinstance(label, str) and len(label) <= 128
            ]

        try:
            return self.approval_service.finish_execution(
                user_id=user_id,
                proposal_id=proposal_id,
                state="completed",
                safe_result=safe_result,
            )
        except Exception as exc:
            logger.exception(
                "Gmail send succeeded but completion could not be persisted."
            )
            try:
                self.approval_service.finish_execution(
                    user_id=user_id,
                    proposal_id=proposal_id,
                    state="execution_uncertain",
                    safe_result={
                        "outcome": "unconfirmed",
                        "reason": "completion_persistence_failed",
                    },
                )
            except Exception:
                logger.exception(
                    "Could not persist the uncertain Gmail send outcome."
                )
            raise ExecutionStatePersistenceError(
                "Gmail may have sent the message, but its result could not "
                "be confirmed in conversation history. Do not retry."
            ) from exc

    @staticmethod
    def _validate_stored_send(
        proposal: ActionApprovalProposal,
    ) -> ApprovalProposalCreate:
        if proposal.action_type != "gmail.send":
            raise PermissionError(
                "Only Gmail send actions can be executed in this phase."
            )
        return ApprovalProposalCreate.model_validate({
            "conversation_id": proposal.conversation_id,
            "source_message_id": proposal.source_message_id,
            "connection_id": proposal.connection_id,
            "action_type": proposal.action_type,
            "parameters": proposal.parameters,
        })

    def _finish_safely(
        self,
        user_id: UUID,
        proposal_id: UUID,
        state: str,
        safe_result: dict[str, Any],
    ) -> ActionApprovalProposal:
        try:
            return self.approval_service.finish_execution(
                user_id=user_id,
                proposal_id=proposal_id,
                state=state,
                safe_result=safe_result,
            )
        except Exception as exc:
            logger.exception(
                "Could not persist Gmail send execution state %s.",
                state,
            )
            raise ExecutionStatePersistenceError(
                "The Gmail send outcome could not be persisted. Do not retry."
            ) from exc


def _safe_reason(error: Exception) -> str:
    if isinstance(error, PermissionError):
        return "permission_or_scope_rejected"
    if isinstance(error, LookupError):
        return "connection_not_available"
    if isinstance(error, ValueError):
        return "stored_action_validation_failed"
    return "executor_error"
