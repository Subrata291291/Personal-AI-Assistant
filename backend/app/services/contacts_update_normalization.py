from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any

# Flat update fields supported by current proposal schema/UI.
_FLAT_UPDATE_FIELDS = (
    "display_name",
    "given_name",
    "family_name",
    "email",
    "phone",
    "organization",
    "job_title",
    "address",
)

# Fields that can be explicitly cleared via `clear_fields` in this phase.
# Clearing removes the targeted field/collection for the contact (or targeted
# unambiguous entry's value where applicable) only when explicitly requested.
_CLEARABLE_FIELDS = {
    "email",        # clears emailAddresses
    "phone",        # clears phoneNumbers
    "address",      # clears addresses
    "organization", # clears organizations
    "job_title",    # clears job title component on targeted organization
}

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class ContactsUpdateValidationError(ValueError):
    """Raised for invalid/ambiguous/unsafe Contacts update payloads."""


@dataclass(frozen=True)
class NormalizedContactsUpdate:
    # Canonical, server-derived parameters to persist in the proposal and execute.
    parameters: dict[str, Any]
    contact_patch: dict[str, Any]
    update_person_fields: tuple[str, ...]


def _is_present_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _clean_str(value: Any) -> str:
    if not isinstance(value, str):
        raise ContactsUpdateValidationError("Invalid field value.")
    cleaned = value.strip()
    if not cleaned:
        # Empty strings are not accepted as updates or clears in current flat schema.
        raise ContactsUpdateValidationError("Field values cannot be empty.")
    return cleaned


def _primary_or_only(items: list[Any] | None, field_label: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    if not isinstance(items, list) or not items:
        return None, []
    normalized = [i for i in items if isinstance(i, dict)]
    if not normalized:
        return None, []
    if len(normalized) == 1:
        return normalized[0], normalized
    primary = [i for i in normalized if (i.get("metadata") or {}).get("primary")]
    if len(primary) == 1:
        return primary[0], normalized
    # Ambiguous: multiple entries and no single primary target.
    raise ContactsUpdateValidationError(
        f"The contact has multiple {field_label} values. Clarify which one to update before creating an approval."
    )


def _pick_single_organization(raw: dict[str, Any]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    orgs = raw.get("organizations")
    return _primary_or_only(orgs if isinstance(orgs, list) else [], "organization")


def _pick_single_email(raw: dict[str, Any]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    emails = raw.get("emailAddresses")
    return _primary_or_only(emails if isinstance(emails, list) else [], "email address")


def _pick_single_phone(raw: dict[str, Any]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    phones = raw.get("phoneNumbers")
    return _primary_or_only(phones if isinstance(phones, list) else [], "phone number")


def _pick_single_address(raw: dict[str, Any]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    addrs = raw.get("addresses")
    return _primary_or_only(addrs if isinstance(addrs, list) else [], "address")


def normalize_contacts_update_parameters(
    *,
    parameters: dict[str, Any],
    current_contact: dict[str, Any],
) -> NormalizedContactsUpdate:
    """Build a safe, deterministic People API update from flat proposal params.

    Semantics (current flat fields):
    - Field not supplied / absent: preserve existing value (not in updatePersonFields).
    - Field supplied with a new non-empty string value: update that specific field/component.
    - Field supplied as empty string / null / empty collection in this flat schema: rejected
      (no silent clear). Explicit clear is not representable by current flat schema.
    - Invalid values or ambiguous multi-value targets: rejected with ContactsUpdateValidationError.

    Multi-value preservation: for a targeted collection (emails/phones/addresses/organizations),
    if the contact has multiple entries and no single unambiguous target (single entry or sole
    primary), the update is rejected rather than guessed. When unambiguous, only the targeted
    entry is modified and all other entries in that collection are preserved.
    """
    if not isinstance(parameters, dict):
        raise ContactsUpdateValidationError("Contacts action parameters are required.")
    if not isinstance(current_contact, dict):
        raise ContactsUpdateValidationError("The current contact could not be loaded for a safe update.")

    raw = current_contact.get("raw") if isinstance(current_contact.get("raw"), dict) else {}
    # Fall back to top-level if raw missing (defensive).
    if not raw:
        raw = current_contact

    requested: dict[str, str] = {}
    for key in _FLAT_UPDATE_FIELDS:
        if key not in parameters:
            continue
        value = parameters.get(key)
        if value is None:
            raise ContactsUpdateValidationError(f"{key} cannot be cleared implicitly.")
        if isinstance(value, (list, dict)):
            raise ContactsUpdateValidationError(f"{key} has an unsupported value.")
        if isinstance(value, str):
            if not value.strip():
                raise ContactsUpdateValidationError(f"{key} cannot be empty.")
            requested[key] = value.strip()
        else:
            raise ContactsUpdateValidationError(f"{key} has an unsupported value.")

    clear_fields_raw = parameters.get("clear_fields")
    clear_fields: list[str] = []
    if clear_fields_raw is not None:
        if not isinstance(clear_fields_raw, list) or not clear_fields_raw:
            raise ContactsUpdateValidationError("clear_fields must be a non-empty list when provided.")
        for item in clear_fields_raw:
            if not isinstance(item, str) or not item.strip():
                raise ContactsUpdateValidationError("clear_fields contains an invalid field.")
            field = item.strip()
            if field not in _CLEARABLE_FIELDS:
                raise ContactsUpdateValidationError(f"{field} cannot be explicitly cleared.")
            if field in requested:
                raise ContactsUpdateValidationError(f"{field} cannot be both updated and cleared.")
            # job_title clear must target organization context; allow if org exists/targetable.
            if field == "organization" and "job_title" in requested:
                raise ContactsUpdateValidationError("organization cannot be cleared when job_title is being updated.")
            if field == "job_title" and "organization" in requested:
                # updating organization name while clearing title is allowed on same org entry.
                pass
            clear_fields.append(field)
        # de-dupe preserving order
        seen_cf: set[str] = set()
        deduped: list[str] = []
        for f in clear_fields:
            if f not in seen_cf:
                seen_cf.add(f)
                deduped.append(f)
        clear_fields = deduped

    if not requested and not clear_fields:
        raise ContactsUpdateValidationError("At least one contact change is required.")

    contact_patch: dict[str, Any] = {}
    update_fields: list[str] = []
    clear_set = set(clear_fields)

    # Names (single primary name entry in People API; preserve unspecified components)
    if any(k in requested for k in ("display_name", "given_name", "family_name")):
        names = raw.get("names") if isinstance(raw.get("names"), list) else []
        existing_name, all_names = _primary_or_only(names, "name") if names else (None, [])
        # If no names exist, create one; if one/primary exists, merge into it preserving components.
        base_name = copy.deepcopy(existing_name) if isinstance(existing_name, dict) else {}
        if "given_name" in requested:
            base_name["givenName"] = requested["given_name"]
        if "family_name" in requested:
            base_name["familyName"] = requested["family_name"]
        if "display_name" in requested:
            base_name["displayName"] = requested["display_name"]
        # If a user provided only display_name and there was no prior name, keep reasonable givenName.
        if "display_name" in requested and "givenName" not in base_name and "familyName" not in base_name and not existing_name:
            base_name.setdefault("givenName", requested["display_name"])
        if existing_name is None:
            new_names = [base_name] if base_name else []
        else:
            target_idx = None
            for idx, item in enumerate(all_names):
                if item is existing_name:
                    target_idx = idx
                    break
            if target_idx is None:
                for idx, item in enumerate(all_names):
                    if (item.get("metadata") or {}).get("primary") and (existing_name.get("metadata") or {}).get("primary"):
                        target_idx = idx
                        break
            new_names = copy.deepcopy(all_names)
            if target_idx is not None:
                new_names[target_idx] = base_name
            else:
                new_names = [base_name] + [n for n in new_names if n != existing_name]
        contact_patch["names"] = new_names
        update_fields.append("names")

    # Email addresses (preserve unrelated entries / explicit clear when requested)
    if "email" in requested or "email" in clear_set:
        target, all_emails = _pick_single_email(raw)
        target_idx = next((i for i, it in enumerate(all_emails) if it is target), None) if target is not None else None
        if "email" in clear_set:
            # Explicit clear: remove all email addresses only when unambiguous target context.
            # If multiple non-primary entries exist, refuse to guess.
            if len(all_emails) > 1 and target is None:
                raise ContactsUpdateValidationError(
                    "The contact has multiple email address values. Clarify which one to clear before creating an approval."
                )
            new_emails = []
        else:
            email_value = requested["email"]
            if not _EMAIL_RE.match(email_value):
                raise ContactsUpdateValidationError("A valid email address is required.")
            new_emails = copy.deepcopy(all_emails)
            if target_idx is None:
                new_emails = [{"value": email_value}]
            else:
                new_emails[target_idx]["value"] = email_value
        contact_patch["emailAddresses"] = new_emails
        update_fields.append("emailAddresses")

    # Phone numbers (preserve unrelated entries / explicit clear when requested)
    if "phone" in requested or "phone" in clear_set:
        target, all_phones = _pick_single_phone(raw)
        target_idx = next((i for i, it in enumerate(all_phones) if it is target), None) if target is not None else None
        if "phone" in clear_set:
            if len(all_phones) > 1 and target is None:
                raise ContactsUpdateValidationError(
                    "The contact has multiple phone number values. Clarify which one to clear before creating an approval."
                )
            new_phones = []
        else:
            phone_value = requested["phone"]
            new_phones = copy.deepcopy(all_phones)
            if target_idx is None:
                new_phones = [{"value": phone_value}]
            else:
                new_phones[target_idx]["value"] = phone_value
        contact_patch["phoneNumbers"] = new_phones
        update_fields.append("phoneNumbers")

    # Organizations/job title (preserve unrelated entries/components; explicit clear when requested)
    if "organization" in requested or "job_title" in requested or "organization" in clear_set or "job_title" in clear_set:
        target, all_orgs = _pick_single_organization(raw)
        target_idx = next((i for i, it in enumerate(all_orgs) if it is target), None) if target is not None else None
        if "organization" in clear_set:
            # Clear full organization collection (and thus job title) explicitly.
            if len(all_orgs) > 1 and target is None:
                raise ContactsUpdateValidationError(
                    "The contact has multiple organization values. Clarify which one to clear before creating an approval."
                )
            new_orgs = []
        else:
            new_orgs = copy.deepcopy(all_orgs)
            if target_idx is None:
                org: dict[str, Any] = {}
                if "organization" in requested:
                    org["name"] = requested["organization"]
                if "job_title" in requested:
                    org["title"] = requested["job_title"]
                elif "job_title" in clear_set:
                    # clearing title with no existing org target is not meaningful
                    pass
                new_orgs = [org] if org else []
            else:
                if "organization" in requested:
                    new_orgs[target_idx]["name"] = requested["organization"]
                if "job_title" in requested:
                    new_orgs[target_idx]["title"] = requested["job_title"]
                elif "job_title" in clear_set:
                    new_orgs[target_idx].pop("title", None)
        contact_patch["organizations"] = new_orgs
        update_fields.append("organizations")

    # Addresses (preserve unrelated entries; explicit clear when requested)
    if "address" in requested or "address" in clear_set:
        target, all_addrs = _pick_single_address(raw)
        target_idx = next((i for i, it in enumerate(all_addrs) if it is target), None) if target is not None else None
        if "address" in clear_set:
            if len(all_addrs) > 1 and target is None:
                raise ContactsUpdateValidationError(
                    "The contact has multiple address values. Clarify which one to clear before creating an approval."
                )
            new_addrs = []
        else:
            new_addrs = copy.deepcopy(all_addrs)
            if target_idx is None:
                new_addrs = [{"formattedValue": requested["address"]}]
            else:
                # Update formatted representation; preserve structured components if present.
                new_addrs[target_idx]["formattedValue"] = requested["address"]
        contact_patch["addresses"] = new_addrs
        update_fields.append("addresses")

    # Deterministic ordering and dedupe of field mask
    seen: set[str] = set()
    ordered_fields: list[str] = []
    for f in update_fields:
        if f not in seen:
            seen.add(f)
            ordered_fields.append(f)

    if not ordered_fields or not contact_patch:
        raise ContactsUpdateValidationError("At least one contact change is required.")

    normalized_params = {
        "contact_id": parameters.get("contact_id"),
        "etag": parameters.get("etag"),
        # Keep original requested flat fields (normalized strings) for traceability/UI compatibility.
        **{k: v for k, v in requested.items()},
    }
    if clear_fields:
        normalized_params["clear_fields"] = clear_fields
    normalized_params["_contacts_update_plan"] = {
        "schema_version": 1,
        "update_person_fields": ",".join(ordered_fields),
        "contact_patch": contact_patch,
    }
    return NormalizedContactsUpdate(
        parameters=normalized_params,
        contact_patch=contact_patch,
        update_person_fields=tuple(ordered_fields),
    )
