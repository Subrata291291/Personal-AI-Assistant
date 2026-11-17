import re
from time import perf_counter
from typing import Any
from urllib.parse import quote
from uuid import UUID

from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.integrations.google.api_client import GoogleAPIClient
from app.integrations.google.exceptions import GoogleAPIError
from app.integrations.google import scopes as google_scopes
from app.repositories.oauth_credential_repository import (
    OAuthCredentialRepository,
)
from app.repositories.user_connection_repository import (
    UserConnectionRepository,
)
from app.core.request_metrics import record_timing

PEOPLE_API_BASE = "https://people.googleapis.com/v1"

DEFAULT_PERSON_FIELDS = (
    "names,emailAddresses,phoneNumbers,organizations,addresses,"
    "birthdays,photos,biographies,metadata,relations,nicknames,userDefined"
)

LIST_PERSON_FIELDS = (
    "names,emailAddresses,phoneNumbers,organizations,photos,metadata,"
    "relations,nicknames,userDefined"
)

DETAIL_PERSON_FIELDS = DEFAULT_PERSON_FIELDS


class ContactsService:
    """Google People API operations for an owned Google account."""

    CONTACTS_READ_SCOPE = google_scopes.CONTACTS_READ_SCOPE
    CONTACTS_WRITE_SCOPE = google_scopes.CONTACTS_WRITE_SCOPE
    CONTACTS_READ_SCOPES = frozenset({
        google_scopes.CONTACTS_READ_SCOPE,
        google_scopes.CONTACTS_WRITE_SCOPE,
    })

    MAX_PAGE_SIZE = 1000
    RESOURCE_NAME_PATTERN = r"^people/[A-Za-z0-9._-]{1,505}\Z"
    _RESOURCE_NAME = re.compile(RESOURCE_NAME_PATTERN)
    _RESOURCE_ID = re.compile(r"[A-Za-z0-9._@+#-]{1,1024}\Z")

    def __init__(self, db: Session):
        self.db = db
        self.connection_repository = UserConnectionRepository(db)
        self.credential_repository = OAuthCredentialRepository(db)
        self.api_client = GoogleAPIClient()

    def _validate_connection(
        self,
        user_id: UUID,
        connection_id: UUID,
        allowed_scopes: frozenset[str],
    ) -> None:
        connection = self.connection_repository.get_active_google_connection(
            user_id=user_id,
            connection_id=connection_id,
        )
        if connection is None:
            raise ValueError("Connected Google account was not found.")

        credential = self.credential_repository.get_by_connection(
            user_id=user_id,
            connection_id=connection_id,
        )
        if credential is None:
            raise ValueError("Google OAuth credentials were not found.")

        if not (set(credential.scopes or []) & allowed_scopes):
            raise PermissionError(
                "The selected Google account is missing Contacts permission."
            )

    def _validate_resource_name(self, resource_name: str) -> str:
        if not isinstance(resource_name, str) or not resource_name.strip():
            raise ValueError("resource_name is required.")
        value = resource_name.strip()
        if not self._RESOURCE_NAME.fullmatch(value):
            raise ValueError("Invalid Contacts resourceName.")
        return value

    def _validate_person_fields(self, person_fields: str | None) -> str:
        if not person_fields or not person_fields.strip():
            return DEFAULT_PERSON_FIELDS
        fields = [item.strip() for item in person_fields.split(",") if item.strip()]
        if not fields:
            return DEFAULT_PERSON_FIELDS
        if any(not re.fullmatch(r"[A-Za-z]+", field) for field in fields):
            raise ValueError("Invalid personFields value.")
        return ",".join(fields)

    @staticmethod
    def _pick_primary(items: list[dict[str, Any]] | None) -> dict[str, Any] | None:
        if not isinstance(items, list):
            return None
        for item in items:
            if isinstance(item, dict) and item.get("metadata", {}).get("primary"):
                return item
        for item in items:
            if isinstance(item, dict):
                return item
        return None

    @classmethod
    def normalize_person(cls, person: dict[str, Any] | None) -> dict[str, Any]:
        person = person if isinstance(person, dict) else {}
        names = person.get("names") if isinstance(person.get("names"), list) else []
        primary_name = cls._pick_primary(names) or {}
        display_name = (
            primary_name.get("displayName")
            or " ".join(
                part for part in [
                    primary_name.get("givenName"),
                    primary_name.get("familyName"),
                ] if isinstance(part, str) and part.strip()
            ).strip()
            or None
        )
        emails = [
            {
                "value": item.get("value"),
                "type": item.get("type"),
                "primary": bool(item.get("metadata", {}).get("primary")),
            }
            for item in (person.get("emailAddresses") or [])
            if isinstance(item, dict) and item.get("value")
        ]
        phones = [
            {
                "value": item.get("value"),
                "type": item.get("type"),
                "primary": bool(item.get("metadata", {}).get("primary")),
            }
            for item in (person.get("phoneNumbers") or [])
            if isinstance(item, dict) and item.get("value")
        ]
        organizations = person.get("organizations") if isinstance(person.get("organizations"), list) else []
        primary_org = cls._pick_primary(organizations) or {}
        addresses = [
            {
                "formatted": item.get("formattedValue") or item.get("formatted_value"),
                "street": item.get("streetAddress"),
                "city": item.get("city"),
                "region": item.get("region"),
                "postal_code": item.get("postalCode"),
                "country": item.get("country"),
                "type": item.get("type"),
                "primary": bool(item.get("metadata", {}).get("primary")),
            }
            for item in (person.get("addresses") or [])
            if isinstance(item, dict)
        ]
        photos = person.get("photos") if isinstance(person.get("photos"), list) else []
        primary_photo = cls._pick_primary(photos) or {}
        photo_url = primary_photo.get("url")
        # Google often appends default markers; treat explicit default as no custom photo.
        if photo_url and primary_photo.get("default"):
            photo_url = None
        relations = [
            {
                "value": item.get("person") or item.get("value"),
                "type": item.get("type"),
                "formatted_type": item.get("formattedType"),
            }
            for item in (person.get("relations") or [])
            if isinstance(item, dict) and (item.get("person") or item.get("value") or item.get("type") or item.get("formattedType"))
        ]
        nicknames = [
            {
                "value": item.get("value"),
                "type": item.get("type"),
            }
            for item in (person.get("nicknames") or [])
            if isinstance(item, dict) and item.get("value")
        ]
        user_defined = [
            {
                "key": item.get("key"),
                "value": item.get("value"),
            }
            for item in (person.get("userDefined") or [])
            if isinstance(item, dict) and (item.get("key") or item.get("value"))
        ]
        return {
            "resource_name": person.get("resourceName"),
            "etag": person.get("etag"),
            "display_name": display_name,
            "given_name": primary_name.get("givenName"),
            "family_name": primary_name.get("familyName"),
            "emails": emails,
            "phones": phones,
            "primary_email": next((e["value"] for e in emails if e.get("primary")), emails[0]["value"] if emails else None),
            "primary_phone": next((p["value"] for p in phones if p.get("primary")), phones[0]["value"] if phones else None),
            "organization": primary_org.get("name"),
            "job_title": primary_org.get("title"),
            "addresses": addresses,
            "primary_address": next((a for a in addresses if a.get("primary")), addresses[0] if addresses else None),
            "photo_url": photo_url,
            "has_photo": bool(photo_url),
            "relations": relations,
            "nicknames": nicknames,
            "user_defined": user_defined,
            "raw": person,
        }

    async def _google_get(
        self,
        user_id: UUID,
        connection_id: UUID,
        endpoint: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        api_db = SessionLocal()
        try:
            result = await self.api_client.request(
                db=api_db,
                user_id=str(user_id),
                connection_id=str(connection_id),
                method="GET",
                url=f"{PEOPLE_API_BASE}/{endpoint}",
                params=params,
            )
            return result if isinstance(result, dict) else {}
        finally:
            api_db.close()

    async def _google_post(
        self,
        user_id: UUID,
        connection_id: UUID,
        endpoint: str,
        json_data: dict[str, Any] | None,
    ) -> dict[str, Any]:
        api_db = SessionLocal()
        try:
            result = await self.api_client.request(
                db=api_db,
                user_id=str(user_id),
                connection_id=str(connection_id),
                method="POST",
                url=f"{PEOPLE_API_BASE}/{endpoint}",
                json_data=json_data,
            )
            return result if isinstance(result, dict) else {}
        finally:
            api_db.close()

    async def _google_patch(
        self,
        user_id: UUID,
        connection_id: UUID,
        endpoint: str,
        json_data: dict[str, Any],
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        api_db = SessionLocal()
        try:
            result = await self.api_client.request(
                db=api_db,
                user_id=str(user_id),
                connection_id=str(connection_id),
                method="PATCH",
                url=f"{PEOPLE_API_BASE}/{endpoint}",
                json_data=json_data,
                params=params,
            )
            return result if isinstance(result, dict) else {}
        finally:
            api_db.close()

    async def _google_delete(
        self,
        user_id: UUID,
        connection_id: UUID,
        endpoint: str,
    ) -> dict[str, Any]:
        api_db = SessionLocal()
        try:
            result = await self.api_client.request(
                db=api_db,
                user_id=str(user_id),
                connection_id=str(connection_id),
                method="DELETE",
                url=f"{PEOPLE_API_BASE}/{endpoint}",
            )
            return result if isinstance(result, dict) else {}
        finally:
            api_db.close()

    async def list_contacts(
        self,
        user_id: UUID,
        connection_id: UUID,
        page_size: int = 100,
        page_token: str | None = None,
        person_fields: str | None = None,
    ) -> dict[str, Any]:
        self._validate_connection(
            user_id, connection_id, self.CONTACTS_READ_SCOPES
        )
        if not 1 <= page_size <= self.MAX_PAGE_SIZE:
            raise ValueError(
                f"page_size must be between 1 and {self.MAX_PAGE_SIZE}."
            )
        fields = self._validate_person_fields(person_fields or LIST_PERSON_FIELDS)
        params: dict[str, Any] = {
            "pageSize": page_size,
            "personFields": fields,
        }
        if page_token:
            params["pageToken"] = page_token

        result = await self._google_get(
            user_id=user_id,
            connection_id=connection_id,
            endpoint="people/me/connections",
            params=params,
        )
        if not isinstance(result, dict):
            raise GoogleAPIError(
                status_code=502,
                message="Google Contacts returned an invalid response.",
            )
        raw_connections = result.get("connections") or []
        contacts = [
            self.normalize_person(person)
            for person in raw_connections
            if isinstance(person, dict)
        ]
        return {
            "contacts": contacts,
            "connections": raw_connections,
            "next_page_token": result.get("nextPageToken"),
            "total_items": result.get("totalItems"),
            "total_people": result.get("totalPeople"),
        }

    async def search_contacts(
        self,
        user_id: UUID,
        connection_id: UUID,
        query: str,
        page_size: int = 30,
        page_token: str | None = None,
        person_fields: str | None = None,
    ) -> dict[str, Any]:
        self._validate_connection(
            user_id, connection_id, self.CONTACTS_READ_SCOPES
        )
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query is required.")
        if not 1 <= page_size <= 30:
            # People searchContacts commonly caps at 30.
            raise ValueError("page_size must be between 1 and 30.")
        fields = self._validate_person_fields(person_fields or LIST_PERSON_FIELDS)
        params: dict[str, Any] = {
            "query": query.strip(),
            "pageSize": page_size,
            "readMask": fields,
        }
        if page_token:
            params["pageToken"] = page_token
        result = await self._google_get(
            user_id=user_id,
            connection_id=connection_id,
            endpoint="people:searchContacts",
            params=params,
        )
        if not isinstance(result, dict):
            raise GoogleAPIError(
                status_code=502,
                message="Google Contacts returned an invalid search response.",
            )
        raw_results = result.get("results") or []
        contacts = []
        for item in raw_results:
            person = item.get("person") if isinstance(item, dict) else None
            if isinstance(person, dict):
                contacts.append(self.normalize_person(person))
        return {
            "contacts": contacts,
            "results": raw_results,
            "next_page_token": result.get("nextPageToken"),
        }

    async def get_contact(
        self,
        user_id: UUID,
        connection_id: UUID,
        resource_name: str,
        person_fields: str | None = None,
    ) -> dict[str, Any]:
        self._validate_connection(
            user_id, connection_id, self.CONTACTS_READ_SCOPES
        )
        resource_name = self._validate_resource_name(resource_name)
        fields = self._validate_person_fields(person_fields or DETAIL_PERSON_FIELDS)
        params = {"personFields": fields}
        result = await self._google_get(
            user_id=user_id,
            connection_id=connection_id,
            endpoint=resource_name,
            params=params,
        )
        if not isinstance(result, dict):
            raise GoogleAPIError(
                status_code=502,
                message="Google Contacts returned an invalid contact.",
            )
        normalized = self.normalize_person(result)
        # Keep raw fields accessible and preserve backward-compatible top-level keys.
        normalized.setdefault("resourceName", result.get("resourceName"))
        return normalized

    async def create_contact(
        self,
        user_id: UUID,
        connection_id: UUID,
        contact: dict[str, Any],
    ) -> dict[str, Any]:
        self._validate_connection(
            user_id, connection_id, {self.CONTACTS_WRITE_SCOPE}
        )
        if not isinstance(contact, dict) or not contact:
            raise ValueError("contact payload is required.")
        result = await self._google_post(
            user_id=user_id,
            connection_id=connection_id,
            endpoint="people:createContact",
            json_data=contact,
        )
        if not isinstance(result, dict):
            raise GoogleAPIError(
                status_code=502,
                message="Google Contacts returned an invalid create response.",
            )
        return result

    async def update_contact(
        self,
        user_id: UUID,
        connection_id: UUID,
        resource_name: str,
        contact: dict[str, Any],
        update_person_fields: str,
        etag: str | None = None,
    ) -> dict[str, Any]:
        self._validate_connection(
            user_id, connection_id, {self.CONTACTS_WRITE_SCOPE}
        )
        resource_name = self._validate_resource_name(resource_name)
        if not isinstance(contact, dict) or not contact:
            raise ValueError("contact payload is required.")
        if not update_person_fields or not update_person_fields.strip():
            raise ValueError("updatePersonFields is required.")
        if etag:
            contact = {**contact, "etag": etag}
        params = {"updatePersonFields": update_person_fields.strip()}
        result = await self._google_patch(
            user_id=user_id,
            connection_id=connection_id,
            endpoint=resource_name,
            json_data=contact,
            params=params,
        )
        if not isinstance(result, dict):
            raise GoogleAPIError(
                status_code=502,
                message="Google Contacts returned an invalid update response.",
            )
        return result

    async def delete_contact(
        self,
        user_id: UUID,
        connection_id: UUID,
        resource_name: str,
    ) -> dict[str, Any]:
        self._validate_connection(
            user_id, connection_id, {self.CONTACTS_WRITE_SCOPE}
        )
        resource_name = self._validate_resource_name(resource_name)
        result = await self._google_delete(
            user_id=user_id,
            connection_id=connection_id,
            endpoint=resource_name,
        )
        return result if isinstance(result, dict) else {}