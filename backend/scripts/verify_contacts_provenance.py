"""Read-only local verification of Google Contacts data provenance.

This script helps investigate reports of suspicious contact fields (for
example placeholder-looking phone numbers or emails) without exposing raw
personal contact data.

What it does
------------
1. Searches the authenticated user's Google Contacts via the existing
   ``ContactsService`` (Google People API ``people:searchContacts``).
2. Selects one candidate (explicitly, or automatically when exactly one
   result is returned).
3. Calls the authoritative ``get_contact`` endpoint with that exact
   ``resourceName``.
4. Compares the raw People API payload, the application-normalized object,
   and the final rendered contact fields using only redacted references and
   value digests.

Safety properties
-----------------
* Read-only: never creates, updates, or deletes contacts.
* Never prints raw phone numbers, emails, addresses, names, OAuth tokens,
  or credentials.
* Resource names, user/connection IDs, and field values are printed only as
  short SHA-256 references/digests.
* Suspicious-pattern reporting uses safe field identifiers and categories
  only (see ``contacts_read._suspicious_contact_patterns``).

Usage (run from the ``backend`` directory)
------------------------------------------
    python -m scripts.verify_contacts_provenance \
        --user-id <uuid> \
        --connection-id <uuid> \
        --query "Contact Name"

If the search returns more than one candidate, re-run with
``--select-index N`` (1-based, as printed by the first run).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from typing import Any
from uuid import UUID

from app.db.session import SessionLocal
from app.services.contacts_service import ContactsService
from app.workflows import contacts_read


def _ref(value: str | None) -> str:
    if not value:
        return "<none>"
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]


def _digest(value: str | None) -> str:
    if not value:
        return "<none>"
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]


def _raw_values(raw: dict[str, Any], field: str) -> list[str]:
    """Extract values from a raw People API person payload (not printed)."""
    values: list[str] = []
    if not isinstance(raw, dict):
        return values
    if field == "phone":
        for item in raw.get("phoneNumbers") or []:
            if isinstance(item, dict) and isinstance(item.get("value"), str):
                values.append(item["value"])
    elif field == "email":
        for item in raw.get("emailAddresses") or []:
            if isinstance(item, dict) and isinstance(item.get("value"), str):
                values.append(item["value"])
    elif field == "organization":
        for item in raw.get("organizations") or []:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                values.append(item["name"])
    elif field == "address":
        for item in raw.get("addresses") or []:
            if not isinstance(item, dict):
                continue
            formatted = item.get("formattedValue")
            if isinstance(formatted, str) and formatted.strip():
                values.append(formatted.strip())
    return list(dict.fromkeys([v.strip() for v in values if v and v.strip()]))


def _normalized_values(contact: dict[str, Any], field: str) -> list[str]:
    return contacts_read._contact_values_for_field(contact, field)


def _raw_as_minimal_contact(raw: dict[str, Any]) -> dict[str, Any]:
    """Shape a raw People API payload like a normalized contact (for pattern checks)."""
    phones = [
        {"value": item.get("value")}
        for item in (raw.get("phoneNumbers") or [])
        if isinstance(item, dict) and item.get("value")
    ]
    emails = [
        {"value": item.get("value")}
        for item in (raw.get("emailAddresses") or [])
        if isinstance(item, dict) and item.get("value")
    ]
    organizations = [
        item for item in (raw.get("organizations") or []) if isinstance(item, dict)
    ]
    addresses = [
        {"formatted": item.get("formattedValue")}
        for item in (raw.get("addresses") or [])
        if isinstance(item, dict) and item.get("formattedValue")
    ]
    return {
        "primary_phone": phones[0]["value"] if phones else None,
        "phones": phones,
        "primary_email": emails[0]["value"] if emails else None,
        "emails": emails,
        "organization": organizations[0].get("name") if organizations else None,
        "addresses": addresses,
    }


def _rendered_contains(rendered: str, values: list[str]) -> bool:
    return all(value in rendered for value in values)


def _compare_field(
    *,
    field: str,
    raw: dict[str, Any],
    normalized: dict[str, Any],
    rendered: str,
) -> dict[str, Any]:
    raw_values = _raw_values(raw, field)
    normalized_values = _normalized_values(normalized, field)
    return {
        "field": field,
        "raw_present": bool(raw_values),
        "normalized_present": bool(normalized_values),
        "rendered_present": _rendered_contains(rendered, normalized_values),
        "raw_value_digests": [_digest(v) for v in raw_values],
        "normalized_value_digests": [_digest(v) for v in normalized_values],
        "raw_matches_normalized": sorted(_digest(v) for v in raw_values)
        == sorted(_digest(v) for v in normalized_values),
        "normalized_rendered_exactly": _rendered_contains(rendered, normalized_values),
    }


def _safe_print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))


async def _verify(
    *,
    user_id: UUID,
    connection_id: UUID,
    query: str,
    select_index: int | None,
) -> int:
    db = SessionLocal()
    try:
        service = ContactsService(db)
        search = await service.search_contacts(
            user_id=user_id,
            connection_id=connection_id,
            query=query,
            page_size=30,
        )
        candidates = [
            c for c in (search.get("contacts") or [])
            if isinstance(c, dict) and isinstance(c.get("resource_name"), str)
        ]
        _safe_print({
            "stage": "search",
            "query_ref": _ref(query),
            "candidate_count": len(candidates),
            "candidate_refs": [
                {
                    "index": index,
                    "resource_ref": _ref(c.get("resource_name")),
                    "display_name_ref": _ref(c.get("display_name")),
                }
                for index, c in enumerate(candidates, start=1)
            ],
        })

        if not candidates:
            _safe_print({"stage": "result", "outcome": "no_match", "verified_source": False})
            return 2

        if select_index is not None:
            if select_index < 1 or select_index > len(candidates):
                _safe_print({
                    "stage": "result",
                    "outcome": "invalid_selection",
                    "candidate_count": len(candidates),
                    "verified_source": False,
                })
                return 2
            selected = candidates[select_index - 1]
        elif len(candidates) == 1:
            selected = candidates[0]
        else:
            _safe_print({
                "stage": "result",
                "outcome": "multiple_candidates_require_selection",
                "candidate_count": len(candidates),
                "verified_source": False,
            })
            return 2

        selected_resource_name = selected.get("resource_name")
        _safe_print({
            "stage": "selection",
            "selected_resource_ref": _ref(selected_resource_name),
            "search_snippet_field_patterns": [
                {"field": field, "pattern": pattern}
                for field, pattern in contacts_read._suspicious_contact_patterns(selected)
            ],
        })

        # Authoritative retrieval: the exact resourceName selected above is the
        # only value passed to get_contact.
        detailed = await service.get_contact(
            user_id=user_id,
            connection_id=connection_id,
            resource_name=selected_resource_name,
        )
        authoritative_resource_name = detailed.get("resource_name") or detailed.get("resourceName")
        resource_match = authoritative_resource_name == selected_resource_name

        minimal = contacts_read._minimal_contact(detailed)
        rendered = contacts_read._format_contact_details(minimal)
        raw = detailed.get("raw") if isinstance(detailed.get("raw"), dict) else {}

        comparisons = [
            _compare_field(field=field, raw=raw, normalized=detailed, rendered=rendered)
            for field in ("phone", "email", "organization", "address")
        ]

        normalized_patterns = [
            {"field": field, "pattern": pattern}
            for field, pattern in contacts_read._suspicious_contact_patterns(minimal)
        ]
        raw_patterns = [
            {"field": field, "pattern": pattern}
            for field, pattern in contacts_read._suspicious_contact_patterns(
                _raw_as_minimal_contact(raw)
            )
        ]

        _safe_print({
            "stage": "authoritative_get_contact",
            "passed_resource_ref": _ref(selected_resource_name),
            "returned_resource_ref": _ref(authoritative_resource_name),
            "resource_name_match": resource_match,
            "api_invocation_status": "invoked",
            "verified_source": bool(resource_match),
            "raw_field_patterns": raw_patterns,
            "normalized_field_patterns": normalized_patterns,
            "field_comparisons": comparisons,
        })

        if not resource_match:
            _safe_print({
                "stage": "result",
                "outcome": "resource_name_mismatch",
                "verified_source": False,
            })
            return 3

        if normalized_patterns or raw_patterns:
            _safe_print({
                "stage": "result",
                "outcome": "authoritative_payload_contains_suspicious_patterns",
                "verified_source": True,
                "note": "Suspicious-looking values are present in the authoritative People API payload; see pattern categories only.",
            })
            return 4

        _safe_print({
            "stage": "result",
            "outcome": "verified",
            "verified_source": True,
        })
        return 0
    except Exception as exc:
        # Never print exception text: it may contain raw contact data or tokens.
        _safe_print({
            "stage": "result",
            "outcome": "error",
            "error_class": type(exc).__name__,
            "verified_source": False,
        })
        return 3
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only, redacted Google Contacts provenance verification.",
    )
    parser.add_argument("--user-id", required=True, help="Authenticated user UUID.")
    parser.add_argument("--connection-id", required=True, help="Google connection UUID.")
    parser.add_argument("--query", required=True, help="Contact search query (not printed).")
    parser.add_argument(
        "--select-index",
        type=int,
        default=None,
        help="1-based candidate index when the search returns multiple results.",
    )
    args = parser.parse_args()

    try:
        user_id = UUID(args.user_id)
        connection_id = UUID(args.connection_id)
    except ValueError:
        print("Invalid --user-id or --connection-id.", file=sys.stderr)
        return 1

    return asyncio.run(
        _verify(
            user_id=user_id,
            connection_id=connection_id,
            query=args.query,
            select_index=args.select_index,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
