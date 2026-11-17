import logging
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.config import settings
from app.integrations.google.exceptions import (
    GoogleAPIError,
    GoogleAPIMutationError,
)
from app.services.action_approval_service import (
    ActionApprovalService,
    ApprovalConflictError,
)
from app.services.contacts_service import ContactsService
from app.services.gmail_action_executor import ExecutionStatePersistenceError


logger = logging.getLogger(__name__)


class ContactsActionExecutor:
    SUPPORTED_ACTION_TYPES = frozenset({
        "contacts.create_contact",
        "contacts.update_contact",
        "contacts.delete_contact",
    })

    def __init__(
        self,
        db: Session,
        approval_service: ActionApprovalService | None = None,
        contacts_service: ContactsService | None = None,
    ):
        self.db = db
        self.approval_service = approval_service or ActionApprovalService(db)
        self.contacts_service = contacts_service or ContactsService(db)

    def _is_enabled(self, action_type: str) -> bool:
        return {
            "contacts.create_contact": settings.CONTACTS_CREATE_ENABLED,
            "contacts.update_contact": settings.CONTACTS_UPDATE_ENABLED,
            "contacts.delete_contact": settings.CONTACTS_DELETE_ENABLED,
        }.get(action_type, False)

    def _verify_integrity(self, *, user_id: UUID, proposal: Any) -> None:
        parameters = proposal.parameters if isinstance(proposal.parameters, dict) else {}
        persisted_fingerprint = getattr(proposal, "payload_fingerprint", None)
        if not isinstance(persisted_fingerprint, str) or not persisted_fingerprint:
            raise ValueError("Contacts proposal integrity verification failed.")
        from app.services.contacts_action_identity import build_contacts_operation_identity

        resource_name = None
        if proposal.action_type in {"contacts.update_contact", "contacts.delete_contact"}:
            candidate = parameters.get("contact_id")
            if isinstance(candidate, str):
                resource_name = candidate
        identity = build_contacts_operation_identity(
            proposal_id=proposal.proposal_id,
            user_id=user_id,
            connection_id=proposal.connection_id,
            action_type=proposal.action_type,
            resource_name=resource_name,
            parameters=parameters,
        )
        if persisted_fingerprint != identity.payload_fingerprint:
            raise ValueError("Contacts proposal integrity verification failed.")

    def _build_create_contact(self, parameters: dict[str, Any]) -> dict[str, Any]:
        display_name = parameters.get("display_name")
        if not isinstance(display_name, str) or not display_name.strip():
            raise ValueError("Invalid Contacts display name.")
        given_name = parameters.get("given_name")
        family_name = parameters.get("family_name")
        name: dict[str, Any] = {}
        if isinstance(given_name, str) and given_name.strip():
            name["givenName"] = given_name.strip()
        if isinstance(family_name, str) and family_name.strip():
            name["familyName"] = family_name.strip()
        if not name:
            name["givenName"] = display_name.strip()
        name["displayName"] = display_name.strip()
        contact: dict[str, Any] = {"names": [name]}
        email = parameters.get("email")
        if isinstance(email, str) and email.strip():
            contact["emailAddresses"] = [{"value": email.strip()}]
        phone = parameters.get("phone")
        if isinstance(phone, str) and phone.strip():
            contact["phoneNumbers"] = [{"value": phone.strip()}]
        organization = parameters.get("organization")
        job_title = parameters.get("job_title")
        if (isinstance(organization, str) and organization.strip()) or (
            isinstance(job_title, str) and job_title.strip()
        ):
            org: dict[str, Any] = {}
            if isinstance(organization, str) and organization.strip():
                org["name"] = organization.strip()
            if isinstance(job_title, str) and job_title.strip():
                org["title"] = job_title.strip()
            contact["organizations"] = [org]
        address = parameters.get("address")
        if isinstance(address, str) and address.strip():
            contact["addresses"] = [{"formattedValue": address.strip()}]
        return contact

    @staticmethod
    def _safe_resource_name(result: Any) -> str | None:
        if not isinstance(result, dict):
            return None
        value = result.get("resource_name")
        return value if isinstance(value, str) and value else None

    def _finish_safely(
        self,
        *,
        user_id: UUID,
        proposal_id: UUID,
        state: str,
        safe_result: dict[str, Any],
    ) -> Any:
        try:
            return self.approval_service.finish_execution(
                user_id=user_id,
                proposal_id=proposal_id,
                state=state,
                safe_result=safe_result,
            )
        except Exception as exc:
            logger.exception(
                "Could not persist Contacts execution state %s for proposal %s.",
                state,
                proposal_id,
            )
            raise ExecutionStatePersistenceError(
                "The Contacts change outcome could not be persisted. Do not retry."
            ) from exc

    async def execute(
        self,
        *,
        user_id: UUID,
        proposal_id: UUID,
    ) -> Any:
        proposal = self.approval_service.get_proposal(user_id, proposal_id)

        if proposal.action_type not in self.SUPPORTED_ACTION_TYPES:
            raise PermissionError("This Contacts action cannot be executed.")
        if not self._is_enabled(proposal.action_type):
            raise PermissionError("Contacts change execution is currently disabled.")

        # Terminal/non-executable states: return existing state without side effects.
        if proposal.status in {"completed", "failed", "rejected", "expired", "cancelled", "execution_uncertain"}:
            return proposal
        if proposal.status == "executing":
            # A previous execution may still be in progress or outcome unknown.
            return proposal

        if proposal.status != "approved":
            raise ApprovalConflictError(
                "Only an approved Contacts proposal can be executed."
            )

        # Verify integrity before claiming to avoid executing tampered payloads.
        self._verify_integrity(user_id=user_id, proposal=proposal)

        claimed = self.approval_service.claim_execution(
            user_id=user_id,
            proposal_id=proposal_id,
        )

        if claimed.action_type not in self.SUPPORTED_ACTION_TYPES:
            raise PermissionError("This Contacts action cannot be executed.")
        if not self._is_enabled(claimed.action_type):
            return self._finish_safely(
                user_id=user_id,
                proposal_id=proposal_id,
                state="failed",
                safe_result={"outcome": "rejected", "reason": "execution_disabled"},
            )

        # Re-verify claimed proposal integrity and binding before external call.
        self._verify_integrity(user_id=user_id, proposal=claimed)
        parameters = claimed.parameters if isinstance(claimed.parameters, dict) else {}
        connection_id = claimed.connection_id

        try:
            if claimed.action_type == "contacts.create_contact":
                contact = self._build_create_contact(parameters)
                result = await self.contacts_service.create_contact(
                    user_id=user_id,
                    connection_id=connection_id,
                    contact=contact,
                )
            elif claimed.action_type == "contacts.update_contact":
                resource_name = parameters.get("contact_id")
                if not isinstance(resource_name, str) or not resource_name:
                    raise ValueError("Invalid Contacts resource name.")
                etag = parameters.get("etag")
                if not isinstance(etag, str) or not etag.strip():
                    raise ValueError("A current contact ETag is required for updates.")

                # Re-check current ETag at execution time for basic optimistic concurrency.
                # (Full conflict recovery/re-approval is out of scope for this phase.)
                current = await self.contacts_service.get_contact(
                    user_id=user_id,
                    connection_id=connection_id,
                    resource_name=resource_name,
                    person_fields=(
                        "names,emailAddresses,phoneNumbers,organizations,addresses,metadata"
                    ),
                )
                current_etag = current.get("etag") or (current.get("raw") or {}).get("etag")
                if isinstance(current_etag, str) and current_etag and current_etag != etag:
                    raise ValueError(
                        "The contact changed since it was loaded. Refresh and try again."
                    )

                plan = parameters.get("_contacts_update_plan")
                if isinstance(plan, dict):
                    update_person_fields = plan.get("update_person_fields")
                    contact_patch = plan.get("contact_patch")
                    if (
                        not isinstance(update_person_fields, str)
                        or not update_person_fields.strip()
                        or not isinstance(contact_patch, dict)
                        or not contact_patch
                    ):
                        raise ValueError("Contacts update proposal is missing a valid normalized update plan.")
                    contact = contact_patch
                    update_fields_str = update_person_fields.strip()
                else:
                    # Legacy fallback (pre-2E.2 proposals): construct from flat fields.
                    contact = {}
                    update_fields: list[str] = []
                    if any(
                        isinstance(parameters.get(key), str) and parameters.get(key).strip()
                        for key in ("display_name", "given_name", "family_name")
                    ):
                        display_name = parameters.get("display_name")
                        given_name = parameters.get("given_name")
                        family_name = parameters.get("family_name")
                        name: dict[str, Any] = {}
                        if isinstance(given_name, str) and given_name.strip():
                            name["givenName"] = given_name.strip()
                        if isinstance(family_name, str) and family_name.strip():
                            name["familyName"] = family_name.strip()
                        if isinstance(display_name, str) and display_name.strip():
                            name["displayName"] = display_name.strip()
                            if "givenName" not in name and " " not in display_name.strip():
                                name["givenName"] = display_name.strip()
                        if name:
                            contact["names"] = [name]
                            update_fields.append("names")
                    email = parameters.get("email")
                    if isinstance(email, str) and email.strip():
                        contact["emailAddresses"] = [{"value": email.strip()}]
                        update_fields.append("emailAddresses")
                    phone = parameters.get("phone")
                    if isinstance(phone, str) and phone.strip():
                        contact["phoneNumbers"] = [{"value": phone.strip()}]
                        update_fields.append("phoneNumbers")
                    organization = parameters.get("organization")
                    job_title = parameters.get("job_title")
                    if (isinstance(organization, str) and organization.strip()) or (
                        isinstance(job_title, str) and job_title.strip()
                    ):
                        org: dict[str, Any] = {}
                        if isinstance(organization, str) and organization.strip():
                            org["name"] = organization.strip()
                        if isinstance(job_title, str) and job_title.strip():
                            org["title"] = job_title.strip()
                        contact["organizations"] = [org]
                        update_fields.append("organizations")
                    address = parameters.get("address")
                    if isinstance(address, str) and address.strip():
                        contact["addresses"] = [{"formattedValue": address.strip()}]
                        update_fields.append("addresses")
                    if not contact or not update_fields:
                        raise ValueError("No Contacts update fields provided.")
                    update_fields_str = ",".join(update_fields)

                result = await self.contacts_service.update_contact(
                    user_id=user_id,
                    connection_id=connection_id,
                    resource_name=resource_name,
                    contact=contact,
                    update_person_fields=update_fields_str,
                    etag=etag.strip(),
                )
            elif claimed.action_type == "contacts.delete_contact":
                resource_name = parameters.get("contact_id")
                if not isinstance(resource_name, str) or not resource_name:
                    raise ValueError("Invalid Contacts resource name.")
                await self.contacts_service.get_contact(
                    user_id=user_id,
                    connection_id=connection_id,
                    resource_name=resource_name,
                    person_fields="names,metadata",
                )
                result = await self.contacts_service.delete_contact(
                    user_id=user_id,
                    connection_id=connection_id,
                    resource_name=resource_name,
                )
            else:
                raise PermissionError("Unsupported Contacts action.")
        except GoogleAPIMutationError as exc:
            state = "execution_uncertain" if exc.outcome_unknown else "failed"
            safe_result: dict[str, Any] = {
                "outcome": "unconfirmed" if exc.outcome_unknown else "rejected",
                "provider_status_code": exc.status_code,
            }
            if exc.reason:
                safe_result["reason"] = exc.reason
            return self._finish_safely(
                user_id=user_id,
                proposal_id=proposal_id,
                state=state,
                safe_result=safe_result,
            )
        except GoogleAPIError as exc:
            # For non-idempotent create, treat timeouts/server/network-class failures
            # as uncertain: the provider may have created the contact.
            uncertain = (
                claimed.action_type == "contacts.create_contact"
                and (exc.status_code == 408 or exc.status_code == 429 or exc.status_code >= 500 or bool(exc.retryable))
            )
            state = "execution_uncertain" if uncertain else "failed"
            safe_result = {
                "outcome": "unconfirmed" if uncertain else "rejected",
                "provider_status_code": exc.status_code,
            }
            return self._finish_safely(
                user_id=user_id,
                proposal_id=proposal_id,
                state=state,
                safe_result=safe_result,
            )
        except (PermissionError, LookupError, ValueError) as exc:
            return self._finish_safely(
                user_id=user_id,
                proposal_id=proposal_id,
                state="failed",
                safe_result={
                    "outcome": "rejected",
                    "reason": type(exc).__name__,
                },
            )
        except Exception as exc:
            logger.exception("Contacts execution failed with an unconfirmed outcome.")
            return self._finish_safely(
                user_id=user_id,
                proposal_id=proposal_id,
                state="execution_uncertain",
                safe_result={
                    "outcome": "unconfirmed",
                    "reason": type(exc).__name__,
                },
            )

        safe_result: dict[str, Any] = {"outcome": "completed"}
        resource_name = self._safe_resource_name(result)
        if resource_name:
            safe_result["resource_name"] = resource_name

        try:
            return self.approval_service.finish_execution(
                user_id=user_id,
                proposal_id=proposal_id,
                state="completed",
                safe_result=safe_result,
            )
        except Exception as exc:
            logger.exception(
                "Contacts change succeeded but completion could not be persisted."
            )
            # Conservative: never auto-retry a possibly completed non-idempotent create.
            uncertain_result: dict[str, Any] = {
                "outcome": "unconfirmed",
                "reason": "completion_persistence_failed",
            }
            if resource_name:
                uncertain_result["resource_name"] = resource_name
            try:
                self.approval_service.finish_execution(
                    user_id=user_id,
                    proposal_id=proposal_id,
                    state="execution_uncertain",
                    safe_result=uncertain_result,
                )
            except Exception:
                logger.exception(
                    "Could not persist uncertain Contacts completion outcome."
                )
            raise ExecutionStatePersistenceError(
                "The Contacts change may have been applied, but its result could not be confirmed. Do not retry."
            ) from exc
