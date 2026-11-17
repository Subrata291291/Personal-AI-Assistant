import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5


CONTACTS_INTEGRITY_SCHEMA_VERSION = 1
CONTACTS_ACTION_TYPES = frozenset({
    "contacts.create_contact",
    "contacts.update_contact",
    "contacts.delete_contact",
})


@dataclass(frozen=True)
class ContactsOperationIdentity:
    proposal_id: UUID
    operation_id: UUID
    payload_fingerprint: str
    action_type: str
    user_id: UUID
    connection_id: UUID
    resource_name: str | None
    integrity_schema_version: int = CONTACTS_INTEGRITY_SCHEMA_VERSION


def _validate_json_value(value: Any, path: str = "parameters") -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite number.")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_value(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} contains a non-string object key.")
            _validate_json_value(item, f"{path}.{key}")
        return
    raise ValueError(f"{path} contains a non-JSON value.")


def canonical_json_bytes(value: Any) -> bytes:
    _validate_json_value(value)
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def contacts_payload_fingerprint(
    *,
    action_type: str,
    user_id: UUID,
    connection_id: UUID,
    resource_name: str | None,
    parameters: dict[str, Any],
    schema_version: int = CONTACTS_INTEGRITY_SCHEMA_VERSION,
) -> str:
    if action_type not in CONTACTS_ACTION_TYPES:
        raise ValueError("Unsupported Contacts action type.")
    canonical = canonical_json_bytes({
        "schema_version": schema_version,
        "action_type": action_type,
        "user_id": str(user_id),
        "connection_id": str(connection_id),
        "resource_name": resource_name,
        "parameters": parameters,
    })
    return hashlib.sha256(canonical).hexdigest()


def contacts_operation_id(
    proposal_id: UUID,
    payload_fingerprint: str,
) -> UUID:
    if not (isinstance(payload_fingerprint, str) and len(payload_fingerprint) == 64):
        raise ValueError("Invalid Contacts payload fingerprint.")
    return uuid5(
        NAMESPACE_URL,
        f"contacts-operation:{proposal_id}:{payload_fingerprint}",
    )


def build_contacts_operation_identity(
    *,
    proposal_id: UUID,
    user_id: UUID,
    connection_id: UUID,
    action_type: str,
    resource_name: str | None,
    parameters: dict[str, Any],
) -> ContactsOperationIdentity:
    fingerprint = contacts_payload_fingerprint(
        action_type=action_type,
        user_id=user_id,
        connection_id=connection_id,
        resource_name=resource_name,
        parameters=parameters,
    )
    operation_id = contacts_operation_id(proposal_id, fingerprint)
    return ContactsOperationIdentity(
        proposal_id=proposal_id,
        operation_id=operation_id,
        payload_fingerprint=fingerprint,
        action_type=action_type,
        user_id=user_id,
        connection_id=connection_id,
        resource_name=resource_name,
    )


def verify_contacts_proposal_integrity(
    proposal: Any,
    authenticated_user_id: UUID,
) -> ContactsOperationIdentity:
    if proposal.user_id != authenticated_user_id:
        raise ValueError("Contacts proposal ownership does not match.")
    resource_name = None
    params = proposal.parameters if isinstance(proposal.parameters, dict) else {}
    # Contacts actions use contact_id/resourceName in parameters; prefer it.
    for key in ("contact_id", "resource_name"):
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            resource_name = value.strip()
            break
    identity = build_contacts_operation_identity(
        proposal_id=proposal.proposal_id,
        user_id=authenticated_user_id,
        connection_id=proposal.connection_id,
        action_type=proposal.action_type,
        resource_name=resource_name,
        parameters=proposal.parameters,
    )
    if proposal.payload_fingerprint != identity.payload_fingerprint:
        raise ValueError("Contacts proposal integrity verification failed.")
    return identity