from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.integrations.google.exceptions import GoogleAPIError
from app.services.contacts_service import ContactsService
from app.services.contacts_update_normalization import (
    ContactsUpdateValidationError,
    normalize_contacts_update_parameters,
)


class ContactsUpdateTargetConflictError(Exception):
    """Raised when client state is stale relative to authoritative contact."""


@dataclass(frozen=True)
class VerifiedContactsUpdateTarget:
    resource_name: str
    etag: str
    parameters: dict[str, Any]
    current_contact: dict[str, Any]


def _resource_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


async def verify_and_bind_contacts_update_target(
    *,
    db: Session,
    user_id: UUID,
    connection_id: UUID,
    parameters: dict[str, Any],
) -> VerifiedContactsUpdateTarget:
    """Fetch and verify the target contact using the server-resolved connection.

    Returns proposal parameters bound to the authoritative server-returned
    resourceName/current ETag and a server-derived normalized update plan.
    Never trusts a client-supplied ETag as authoritative.
    """
    if not isinstance(parameters, dict):
        raise ValueError("Contacts action parameters are required.")

    requested_contact_id = _resource_name(parameters.get("contact_id"))
    if not requested_contact_id:
        raise ValueError("A valid contact target is required.")

    client_etag = parameters.get("etag")
    if not isinstance(client_etag, str) or not client_etag.strip():
        raise ValueError("A current contact ETag is required for updates.")

    service = ContactsService(db)
    try:
        current = await service.get_contact(
            user_id=user_id,
            connection_id=connection_id,
            resource_name=requested_contact_id,
            person_fields="names,emailAddresses,phoneNumbers,organizations,addresses,metadata",
        )
    except GoogleAPIError:
        raise
    except PermissionError:
        raise
    except ValueError:
        raise
    except Exception as exc:
        # Preserve explicit verification failure without leaking internals.
        raise RuntimeError("Unable to verify the selected contact.") from exc

    if not isinstance(current, dict):
        raise LookupError("The selected contact could not be verified.")

    server_resource_name = _resource_name(current.get("resource_name")) or _resource_name(
        current.get("resourceName")
    ) or _resource_name((current.get("raw") or {}).get("resourceName"))
    if not server_resource_name:
        raise LookupError("The selected contact could not be verified.")
    if server_resource_name != requested_contact_id:
        raise LookupError("The selected contact could not be verified for this account.")

    server_etag = _resource_name(current.get("etag")) or _resource_name(
        (current.get("raw") or {}).get("etag")
    )
    if not server_etag:
        raise LookupError("The selected contact could not be verified. Refresh and try again.")

    if client_etag.strip() != server_etag:
        raise ContactsUpdateTargetConflictError(
            "The contact changed since it was loaded. Refresh the contact and review your changes before creating a new update approval."
        )

    # Build normalized, deterministic update plan against the verified current contact.
    # This is the authoritative proposal/execution payload for supported flat fields.
    normalized = normalize_contacts_update_parameters(
        parameters=parameters,
        current_contact=current,
    )
    bound = dict(normalized.parameters)
    # Bind trusted server-verified identity/version (override any client value).
    bound["contact_id"] = server_resource_name
    bound["etag"] = server_etag
    return VerifiedContactsUpdateTarget(
        resource_name=server_resource_name,
        etag=server_etag,
        parameters=bound,
        current_contact=current,
    )
