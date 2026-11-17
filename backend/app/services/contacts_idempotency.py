from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from app.schemas.action_approval import ApprovalProposalCreate


CONTACTS_CREATE_IDEMPOTENCY_NORMALIZATION_VERSION = 1
CONTACTS_CREATE_IDEMPOTENCY_HASH_ALG = "sha256"
CONTACTS_CREATE_IDEMPOTENCY_KEY_MAX_LENGTH = 128
CONTACTS_CREATE_IDEMPOTENCY_REPLAY_DAYS = 30
_IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}\Z")


@dataclass(frozen=True)
class ContactsCreateIdempotencyContext:
    tenant_id: UUID
    user_id: UUID
    connection_id: UUID
    action_type: str
    idempotency_key: str
    normalized_parameters: dict[str, Any]
    payload_fingerprint: str
    normalization_version: int = CONTACTS_CREATE_IDEMPOTENCY_NORMALIZATION_VERSION
    request_hash_alg: str = CONTACTS_CREATE_IDEMPOTENCY_HASH_ALG


def validate_contacts_create_idempotency_key(value: str | None) -> str:
    if value is None:
        raise ValueError("Idempotency-Key header is required for Contacts create proposals.")
    key = value.strip()
    if not key:
        raise ValueError("Idempotency-Key header is required for Contacts create proposals.")
    if len(key) > CONTACTS_CREATE_IDEMPOTENCY_KEY_MAX_LENGTH:
        raise ValueError("Idempotency-Key is too long.")
    if not _IDEMPOTENCY_KEY_PATTERN.fullmatch(key):
        raise ValueError("Idempotency-Key contains invalid characters.")
    return key


def derive_contacts_tenant_id(user_id: UUID) -> UUID:
    # Phase 2C.1: no first-class tenant/workspace entity exists; authenticated
    # user identity is the effective server-resolved isolation boundary.
    return user_id


def normalize_contacts_create_parameters(parameters: dict[str, Any]) -> dict[str, Any]:
    """Return validated, normalized Contacts create parameters.

    Uses ApprovalProposalCreate so normalization matches existing proposal
    validation without trusting client-supplied fingerprints.
    """
    if not isinstance(parameters, dict):
        raise ValueError("Contacts action parameters are required.")
    sanitized = dict(parameters)
    # Never trust frontend-supplied fingerprints/hashes.
    sanitized.pop("payload_fingerprint", None)
    sanitized.pop("request_hash", None)
    sanitized.pop("fingerprint", None)
    validated = ApprovalProposalCreate.model_validate({
        "conversation_id": UUID("00000000-0000-0000-0000-000000000001"),
        "source_message_id": UUID("00000000-0000-0000-0000-000000000002"),
        "connection_id": UUID("00000000-0000-0000-0000-000000000003"),
        "action_type": "contacts.create_contact",
        "parameters": sanitized,
    })
    normalized = validated.parameters
    if not isinstance(normalized, dict):
        raise ValueError("Contacts action parameters are invalid.")
    return normalized


def canonical_contacts_create_fingerprint_payload(
    *,
    tenant_id: UUID,
    user_id: UUID,
    connection_id: UUID,
    action_type: str,
    normalized_parameters: dict[str, Any],
    normalization_version: int = CONTACTS_CREATE_IDEMPOTENCY_NORMALIZATION_VERSION,
) -> dict[str, Any]:
    if action_type != "contacts.create_contact":
        raise ValueError("Unsupported Contacts action type for idempotency.")
    return {
        "v": normalization_version,
        "tenant_id": str(tenant_id),
        "user_id": str(user_id),
        "connection_id": str(connection_id),
        "action_type": action_type,
        "parameters": normalized_parameters,
    }


def contacts_create_payload_fingerprint(
    *,
    tenant_id: UUID,
    user_id: UUID,
    connection_id: UUID,
    action_type: str,
    normalized_parameters: dict[str, Any],
    normalization_version: int = CONTACTS_CREATE_IDEMPOTENCY_NORMALIZATION_VERSION,
) -> str:
    payload = canonical_contacts_create_fingerprint_payload(
        tenant_id=tenant_id,
        user_id=user_id,
        connection_id=connection_id,
        action_type=action_type,
        normalized_parameters=normalized_parameters,
        normalization_version=normalization_version,
    )
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def build_contacts_create_idempotency_context(
    *,
    user_id: UUID,
    connection_id: UUID,
    action_type: str,
    idempotency_key: str | None,
    parameters: dict[str, Any],
) -> ContactsCreateIdempotencyContext:
    key = validate_contacts_create_idempotency_key(idempotency_key)
    if action_type != "contacts.create_contact":
        raise ValueError("Idempotency is only supported for contacts.create_contact.")
    tenant_id = derive_contacts_tenant_id(user_id)
    normalized_parameters = normalize_contacts_create_parameters(parameters)
    fingerprint = contacts_create_payload_fingerprint(
        tenant_id=tenant_id,
        user_id=user_id,
        connection_id=connection_id,
        action_type=action_type,
        normalized_parameters=normalized_parameters,
    )
    return ContactsCreateIdempotencyContext(
        tenant_id=tenant_id,
        user_id=user_id,
        connection_id=connection_id,
        action_type=action_type,
        idempotency_key=key,
        normalized_parameters=normalized_parameters,
        payload_fingerprint=fingerprint,
    )


def contacts_create_replay_until(now: datetime | None = None) -> datetime:
    base = now or datetime.now(timezone.utc)
    # Conservative replay window; must not be shorter than proposal expiry and
    # should remain safe for audit/replay. Uncertain outcomes require manual
    # resolution and must not be auto-retried regardless of replay window.
    return base + timedelta(days=CONTACTS_CREATE_IDEMPOTENCY_REPLAY_DAYS)
