import hashlib
import json
import math
import re
from dataclasses import dataclass
from uuid import NAMESPACE_URL, UUID, uuid5
from typing import Any


CALENDAR_INTEGRITY_SCHEMA_VERSION = 1
CALENDAR_ACTION_TYPES = frozenset({
    "calendar.create_event",
    "calendar.update_event",
    "calendar.delete_event",
})
_CALENDAR_ID = re.compile(r"[A-Za-z0-9._@+-]{1,1024}\Z")
_GOOGLE_EVENT_ID = re.compile(r"[0-9a-v]{5,1024}\Z")


class CalendarOperationConflictError(Exception):
    """Raised when an operation identity is reused for different content."""


@dataclass(frozen=True)
class CalendarOperationIdentity:
    proposal_id: UUID
    operation_id: UUID
    payload_fingerprint: str
    action_type: str
    user_id: UUID
    connection_id: UUID
    calendar_id: str
    event_id: str | None
    integrity_schema_version: int = CALENDAR_INTEGRITY_SCHEMA_VERSION


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


def calendar_payload_fingerprint(
    *,
    action_type: str,
    user_id: UUID,
    connection_id: UUID,
    calendar_id: str,
    parameters: dict[str, Any],
    schema_version: int = CALENDAR_INTEGRITY_SCHEMA_VERSION,
) -> str:
    if action_type not in CALENDAR_ACTION_TYPES:
        raise ValueError("Unsupported Calendar action type.")
    if (
        not isinstance(calendar_id, str)
        or calendar_id in {".", ".."}
        or not _CALENDAR_ID.fullmatch(calendar_id)
    ):
        raise ValueError("Invalid Calendar ID.")
    canonical = canonical_json_bytes({
        "schema_version": schema_version,
        "action_type": action_type,
        "user_id": str(user_id),
        "connection_id": str(connection_id),
        "calendar_id": calendar_id,
        "parameters": parameters,
    })
    return hashlib.sha256(canonical).hexdigest()


def calendar_operation_id(
    proposal_id: UUID,
    payload_fingerprint: str,
) -> UUID:
    if not re.fullmatch(r"[a-f0-9]{64}", payload_fingerprint):
        raise ValueError("Invalid Calendar payload fingerprint.")
    return uuid5(
        NAMESPACE_URL,
        f"calendar-operation:{proposal_id}:{payload_fingerprint}",
    )


def calendar_create_event_id(
    operation_id: UUID,
    payload_fingerprint: str,
) -> str:
    if not re.fullmatch(r"[a-f0-9]{64}", payload_fingerprint):
        raise ValueError("Invalid Calendar payload fingerprint.")
    digest = hashlib.sha256(
        f"calendar-event-v1:{operation_id}:{payload_fingerprint}".encode(
            "ascii",
        )
    ).hexdigest()
    event_id = f"pa{digest}"
    if not _GOOGLE_EVENT_ID.fullmatch(event_id):
        raise ValueError("Generated Calendar event ID is invalid.")
    return event_id


def build_calendar_operation_identity(
    *,
    proposal_id: UUID,
    user_id: UUID,
    connection_id: UUID,
    action_type: str,
    parameters: dict[str, Any],
) -> CalendarOperationIdentity:
    calendar_id = parameters.get("calendar_id")
    fingerprint = calendar_payload_fingerprint(
        action_type=action_type,
        user_id=user_id,
        connection_id=connection_id,
        calendar_id=calendar_id,
        parameters=parameters,
    )
    operation_id = calendar_operation_id(proposal_id, fingerprint)
    event_id = (
        calendar_create_event_id(operation_id, fingerprint)
        if action_type == "calendar.create_event"
        else None
    )
    return CalendarOperationIdentity(
        proposal_id=proposal_id,
        operation_id=operation_id,
        payload_fingerprint=fingerprint,
        action_type=action_type,
        user_id=user_id,
        connection_id=connection_id,
        calendar_id=calendar_id,
        event_id=event_id,
    )


def verify_calendar_proposal_integrity(
    proposal: Any,
    authenticated_user_id: UUID,
) -> CalendarOperationIdentity:
    if proposal.user_id != authenticated_user_id:
        raise CalendarOperationConflictError(
            "Calendar proposal ownership does not match the authenticated user."
        )
    identity = build_calendar_operation_identity(
        proposal_id=proposal.proposal_id,
        user_id=authenticated_user_id,
        connection_id=proposal.connection_id,
        action_type=proposal.action_type,
        parameters=proposal.parameters,
    )
    if (
        proposal.integrity_schema_version
        != identity.integrity_schema_version
        or proposal.payload_fingerprint != identity.payload_fingerprint
        or proposal.operation_id != identity.operation_id
        or proposal.calendar_event_id != identity.event_id
    ):
        raise CalendarOperationConflictError(
            "Calendar proposal integrity verification failed."
        )
    return identity


def validate_idempotency_fingerprint_reuse(
    existing_fingerprint: str,
    requested_fingerprint: str,
) -> None:
    if existing_fingerprint != requested_fingerprint:
        raise CalendarOperationConflictError(
            "This idempotency key already belongs to a different Calendar action."
        )


def verify_calendar_event_operation_marker(
    event: dict[str, Any],
    identity: CalendarOperationIdentity,
) -> None:
    if identity.event_id is None:
        raise ValueError("This Calendar operation has no create-event ID.")
    private_properties = (
        (event.get("extendedProperties") or {}).get("private") or {}
    )
    if (
        event.get("id") != identity.event_id
        or private_properties.get("personal_ai_operation_id")
        != str(identity.operation_id)
        or private_properties.get("personal_ai_payload_fingerprint")
        != identity.payload_fingerprint
    ):
        raise CalendarOperationConflictError(
            "Calendar event ID is already bound to a different operation."
        )
