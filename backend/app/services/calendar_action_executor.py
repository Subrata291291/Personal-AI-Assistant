import logging
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import SessionLocal
from app.integrations.google.api_client import GoogleAPIClient
from app.integrations.google.exceptions import (
    GoogleAPIError,
    GoogleAPIMutationError,
)
from app.repositories.action_approval_repository import (
    CalendarActionOperationRepository,
)
from app.schemas.action_approval import ActionApprovalProposal
from app.schemas.calendar_approval import CalendarCreateEventParameters
from app.services.action_approval_service import (
    ActionApprovalService,
    ApprovalConflictError,
)
from app.services.calendar_action_identity import (
    CalendarOperationConflictError,
    CalendarOperationIdentity,
    build_calendar_operation_identity,
    verify_calendar_event_operation_marker,
)
from app.services.calendar_service import CalendarService


logger = logging.getLogger(__name__)


class CalendarExecutionStatePersistenceError(Exception):
    """Raised when a provider result cannot be durably recorded."""


class CalendarActionExecutor:
    SUPPORTED_ACTION_TYPES = frozenset({"calendar.create_event"})
    RECONCILE_AFTER_SECONDS = 120

    def __init__(
        self,
        db: Session,
        approval_service: ActionApprovalService | None = None,
        calendar_service: CalendarService | None = None,
        api_client: GoogleAPIClient | None = None,
    ):
        self.db = db
        self.approval_service = approval_service or ActionApprovalService(db)
        self.calendar_service = calendar_service or CalendarService(db)
        self.api_client = api_client or GoogleAPIClient()
        self.operation_repository = (
            self.approval_service.calendar_operation_repository
            if self.approval_service
            else CalendarActionOperationRepository()
        )

    async def execute(
        self,
        *,
        user_id: UUID,
        proposal_id: UUID,
    ) -> ActionApprovalProposal:
        if not settings.CALENDAR_CREATE_EVENT_ENABLED:
            raise PermissionError(
                "Google Calendar event creation is not enabled."
            )
        proposal = self.approval_service.get_proposal(user_id, proposal_id)
        if proposal.action_type not in self.SUPPORTED_ACTION_TYPES:
            raise PermissionError(
                "Only Calendar event creation can be executed."
            )
        if proposal.status != "approved":
            raise ApprovalConflictError(
                "Only an approved Calendar event proposal can be executed."
            )
        parameters = CalendarCreateEventParameters.model_validate(
            proposal.parameters
        )
        identity = build_calendar_operation_identity(
            proposal_id=proposal.proposal_id,
            user_id=user_id,
            connection_id=proposal.connection_id,
            action_type=proposal.action_type,
            parameters=parameters.model_dump(mode="json"),
        )
        if (
            identity.operation_id != proposal.operation_id
            or identity.payload_fingerprint != proposal.payload_fingerprint
            or identity.event_id != proposal.calendar_event_id
        ):
            raise ApprovalConflictError(
                "Calendar approval integrity verification failed."
            )

        await self.calendar_service.validate_create_event_access(
            user_id=user_id,
            connection_id=proposal.connection_id,
            calendar_id=parameters.calendar_id,
        )
        claimed = self.approval_service.claim_calendar_execution(
            user_id=user_id,
            proposal_id=proposal_id,
        )
        return await self._insert_and_reconcile(
            user_id=user_id,
            proposal=claimed,
            identity=identity,
            parameters=parameters,
        )

    async def _insert_and_reconcile(
        self,
        *,
        user_id: UUID,
        proposal: ActionApprovalProposal,
        identity: CalendarOperationIdentity,
        parameters: CalendarCreateEventParameters,
    ) -> ActionApprovalProposal:
        url = (
            f"{CalendarService.CALENDAR_API_BASE}/calendars/"
            f"{quote(parameters.calendar_id, safe='')}/events"
        )
        body = self._event_body(identity, parameters)
        query = {"sendUpdates": "all" if parameters.attendees else "none"}
        api_db = SessionLocal()
        try:
            await self.api_client.request_mutation(
                db=api_db,
                user_id=str(user_id),
                connection_id=str(proposal.connection_id),
                method="POST",
                url=url,
                params=query,
                json_data=body,
            )
        except GoogleAPIMutationError as exc:
            if exc.status_code == 409 or exc.outcome_unknown:
                return await self._reconcile_after_attempt(
                    user_id=user_id,
                    proposal=proposal,
                    identity=identity,
                    parameters=parameters,
                    reason=(
                        "duplicate_event_id"
                        if exc.status_code == 409
                        else "provider_outcome_unknown"
                    ),
                    expected_state="executing",
                )
            return self._finish(
                user_id=user_id,
                proposal_id=proposal.proposal_id,
                expected_state="executing",
                state="failed",
                classification=(
                    "not_transmitted"
                    if exc.reason == "request_not_transmitted"
                    else "rejected"
                ),
                reconciliation="not_required",
                result={
                    "outcome": "rejected",
                    "provider_status_code": exc.status_code,
                },
            )
        except GoogleAPIError as exc:
            return self._finish(
                user_id=user_id,
                proposal_id=proposal.proposal_id,
                expected_state="executing",
                state="failed",
                classification="rejected",
                reconciliation="not_required",
                result={
                    "outcome": "rejected",
                    "provider_status_code": exc.status_code,
                },
            )
        except Exception as exc:
            logger.exception(
                "Calendar create request outcome could not be recorded "
                "| proposal_id=%s",
                proposal.proposal_id,
            )
            return await self._reconcile_after_attempt(
                user_id=user_id,
                proposal=proposal,
                identity=identity,
                parameters=parameters,
                reason="local_or_transport_error",
                expected_state="executing",
                cause=exc,
            )
        finally:
            api_db.close()

        return await self._reconcile_after_attempt(
            user_id=user_id,
            proposal=proposal,
            identity=identity,
            parameters=parameters,
            reason="insert_accepted",
            expected_state="executing",
        )

    @staticmethod
    def _event_body(
        identity: CalendarOperationIdentity,
        parameters: CalendarCreateEventParameters,
    ) -> dict[str, Any]:
        if identity.event_id is None:
            raise ValueError("Create operation is missing a deterministic ID.")
        if parameters.all_day:
            start: dict[str, str] = {"date": parameters.start}
            end: dict[str, str] = {"date": parameters.end}
        else:
            start = {
                "dateTime": parameters.start,
                "timeZone": parameters.time_zone or "",
            }
            end = {
                "dateTime": parameters.end,
                "timeZone": parameters.time_zone or "",
            }
        event: dict[str, Any] = {
            "id": identity.event_id,
            "summary": parameters.summary,
            "start": start,
            "end": end,
            "extendedProperties": {
                "private": {
                    "personal_ai_operation_id": str(identity.operation_id),
                    "personal_ai_payload_fingerprint": (
                        identity.payload_fingerprint
                    ),
                },
            },
        }
        if parameters.description is not None:
            event["description"] = parameters.description
        if parameters.location is not None:
            event["location"] = parameters.location
        if parameters.attendees:
            event["attendees"] = [
                {"email": address} for address in parameters.attendees
            ]
        return event

    async def _reconcile_after_attempt(
        self,
        *,
        user_id: UUID,
        proposal: ActionApprovalProposal,
        identity: CalendarOperationIdentity,
        parameters: CalendarCreateEventParameters,
        reason: str,
        expected_state: str,
        cause: Exception | None = None,
    ) -> ActionApprovalProposal:
        event = await self._read_back(
            user_id=user_id,
            connection_id=proposal.connection_id,
            identity=identity,
            calendar_id=parameters.calendar_id,
        )
        if event is not None:
            try:
                verify_calendar_event_operation_marker(event, identity)
                self._verify_event_fields(event, parameters)
            except (CalendarOperationConflictError, ValueError, TypeError):
                return self._finish(
                    user_id=user_id,
                    proposal_id=proposal.proposal_id,
                    expected_state=expected_state,
                    state="execution_uncertain",
                    classification="outcome_unknown",
                    reconciliation="manual_review",
                    result={
                        "outcome": "unconfirmed",
                        "reason": "event_does_not_match_approved_request",
                    },
                )
            return self._finish(
                user_id=user_id,
                proposal_id=proposal.proposal_id,
                expected_state=expected_state,
                state="completed",
                classification="confirmed",
                reconciliation="confirmed",
                result={
                    "outcome": "created",
                    "google_event_id": identity.event_id or "",
                    "html_link": self._safe_event_link(event.get("htmlLink")),
                },
            )

        if cause is not None:
            logger.warning(
                "Calendar event read-back did not confirm the provider "
                "result | proposal_id=%s | cause=%s",
                proposal.proposal_id,
                type(cause).__name__,
            )
        return self._finish(
            user_id=user_id,
            proposal_id=proposal.proposal_id,
            expected_state=expected_state,
            state="execution_uncertain",
            classification="outcome_unknown",
            reconciliation="pending",
            result={
                "outcome": "unconfirmed",
                "reason": (
                    "provider_readback_not_found"
                    if reason == "insert_accepted"
                    else "provider_outcome_requires_reconciliation"
                ),
            },
        )

    async def _read_back(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        identity: CalendarOperationIdentity,
        calendar_id: str,
    ) -> dict[str, Any] | None:
        if identity.event_id is None:
            raise ValueError("Create operation is missing a deterministic ID.")
        try:
            result = await self.calendar_service._google_get(
                user_id=user_id,
                connection_id=connection_id,
                endpoint=(
                    f"calendars/{quote(calendar_id, safe='')}/events/"
                    f"{quote(identity.event_id, safe='')}"
                ),
                params={},
            )
        except GoogleAPIError as exc:
            if exc.status_code == 404:
                return None
            logger.warning(
                "Calendar event read-back failed | status=%s",
                exc.status_code,
            )
            return None
        if not isinstance(result, dict):
            return None
        return result

    async def reconcile(
        self,
        *,
        user_id: UUID,
        proposal_id: UUID,
    ) -> ActionApprovalProposal:
        proposal = self.approval_service.get_proposal(user_id, proposal_id)
        if proposal.action_type != "calendar.create_event":
            raise PermissionError(
                "Only Calendar event creation can be reconciled."
            )
        if proposal.status not in {"execution_uncertain", "executing"}:
            return proposal
        if proposal.status == "executing":
            started_at = proposal.execution_started_at
            if (
                started_at is None
                or (datetime.now(started_at.tzinfo) - started_at).total_seconds()
                < self.RECONCILE_AFTER_SECONDS
            ):
                return proposal
            proposal = self.operation_repository.mark_stale_execution_uncertain(
                user_id=user_id,
                proposal_id=proposal_id,
                stale_after_seconds=self.RECONCILE_AFTER_SECONDS,
            )
            if proposal is None:
                return self.approval_service.get_proposal(user_id, proposal_id)

        parameters = CalendarCreateEventParameters.model_validate(
            proposal.parameters
        )
        identity = build_calendar_operation_identity(
            proposal_id=proposal.proposal_id,
            user_id=user_id,
            connection_id=proposal.connection_id,
            action_type=proposal.action_type,
            parameters=parameters.model_dump(mode="json"),
        )
        event = await self._read_back(
            user_id=user_id,
            connection_id=proposal.connection_id,
            identity=identity,
            calendar_id=parameters.calendar_id,
        )
        if event is None:
            self.operation_repository.update_uncertain_reconciliation(
                user_id=user_id,
                proposal_id=proposal_id,
                reconciliation_status="pending",
            )
            return self.approval_service.get_proposal(user_id, proposal_id)
        try:
            verify_calendar_event_operation_marker(event, identity)
            self._verify_event_fields(event, parameters)
        except (CalendarOperationConflictError, ValueError, TypeError):
            self.operation_repository.update_uncertain_reconciliation(
                user_id=user_id,
                proposal_id=proposal_id,
                reconciliation_status="manual_review",
            )
            return self.approval_service.get_proposal(user_id, proposal_id)
        return self._finish(
            user_id=user_id,
            proposal_id=proposal_id,
            expected_state="execution_uncertain",
            state="completed",
            classification="confirmed",
            reconciliation="confirmed",
            result={
                "outcome": "created",
                "google_event_id": identity.event_id or "",
                "html_link": self._safe_event_link(event.get("htmlLink")),
            },
        )

    @staticmethod
    def _verify_event_fields(
        event: dict[str, Any],
        parameters: CalendarCreateEventParameters,
    ) -> None:
        if event.get("summary") != parameters.summary:
            raise ValueError("Provider event summary does not match.")
        if event.get("description") != parameters.description:
            raise ValueError("Provider event description does not match.")
        if event.get("location") != parameters.location:
            raise ValueError("Provider event location does not match.")

        start = event.get("start")
        end = event.get("end")
        if not isinstance(start, dict) or not isinstance(end, dict):
            raise ValueError("Provider event time is missing.")
        if parameters.all_day:
            if (
                start.get("date") != parameters.start
                or end.get("date") != parameters.end
            ):
                raise ValueError("Provider all-day date range does not match.")
        else:
            if (
                start.get("timeZone") != parameters.time_zone
                or end.get("timeZone") != parameters.time_zone
            ):
                raise ValueError("Provider event timezone does not match.")
            if (
                CalendarActionExecutor._utc_datetime(start.get("dateTime"))
                != CalendarActionExecutor._utc_datetime(parameters.start)
                or CalendarActionExecutor._utc_datetime(end.get("dateTime"))
                != CalendarActionExecutor._utc_datetime(parameters.end)
            ):
                raise ValueError("Provider event time range does not match.")

        actual_attendees = event.get("attendees") or []
        if not isinstance(actual_attendees, list):
            raise ValueError("Provider attendees are invalid.")
        actual_emails = {
            attendee.get("email", "").casefold()
            for attendee in actual_attendees
            if isinstance(attendee, dict)
            and isinstance(attendee.get("email"), str)
        }
        expected_emails = {
            address.casefold() for address in parameters.attendees
        }
        if actual_emails != expected_emails:
            raise ValueError("Provider attendees do not match.")

    @staticmethod
    def _utc_datetime(value: object) -> datetime:
        if not isinstance(value, str):
            raise ValueError("Provider event datetime is invalid.")
        parsed = datetime.fromisoformat(
            value[:-1] + "+00:00" if value.endswith("Z") else value
        )
        if parsed.utcoffset() is None:
            raise ValueError("Provider event datetime is not timezone-aware.")
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _safe_event_link(value: object) -> str | None:
        if not isinstance(value, str) or len(value) > 2048:
            return None
        if not value.startswith("https://www.google.com/calendar/event?"):
            return None
        return value

    def _finish(
        self,
        *,
        user_id: UUID,
        proposal_id: UUID,
        expected_state: str,
        state: str,
        classification: str,
        reconciliation: str,
        result: dict[str, Any],
    ) -> ActionApprovalProposal:
        try:
            return self.approval_service.finish_calendar_execution(
                user_id,
                proposal_id,
                expected_state=expected_state,
                state=state,
                provider_result_classification=classification,
                reconciliation_status=reconciliation,
                safe_result=result,
            )
        except Exception as exc:
            logger.exception(
                "Calendar execution result persistence failed "
                "| proposal_id=%s | target_state=%s",
                proposal_id,
                state,
            )
            raise CalendarExecutionStatePersistenceError(
                "The Calendar event result could not be saved. Do not retry; "
                "check the proposal status for reconciliation."
            ) from exc
