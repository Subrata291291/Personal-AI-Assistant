from uuid import UUID

from sqlalchemy.orm import Session

from app.integrations.google import scopes as google_scopes
from app.core.config import settings
from app.repositories.oauth_credential_repository import (
    OAuthCredentialRepository,
)
from app.repositories.user_connection_repository import (
    UserConnectionRepository,
)
from app.services.oauth_state_service import OAuthStateService


class UserConnectionService:
    """
    Business logic for external account connections.

    Handles connection creation and reconnection.
    """

    GMAIL_READ_SCOPE = (
        "https://www.googleapis.com/auth/gmail.readonly"
    )
    CALENDAR_EVENTS_READ_SCOPE = google_scopes.CALENDAR_EVENTS_READ_SCOPE
    CALENDAR_EVENTS_WRITE_SCOPE = google_scopes.CALENDAR_EVENTS_WRITE_SCOPE
    CALENDAR_LIST_READ_SCOPES = google_scopes.CALENDAR_LIST_READ_SCOPES
    CALENDAR_FREEBUSY_SCOPE = google_scopes.CALENDAR_FREEBUSY_SCOPE
    CALENDAR_EVENT_READ_SCOPES = google_scopes.CALENDAR_EVENT_READ_SCOPES
    CALENDAR_FREEBUSY_SCOPES = google_scopes.CALENDAR_FREEBUSY_SCOPES
    CONTACTS_READ_SCOPE = google_scopes.CONTACTS_READ_SCOPE
    CONTACTS_WRITE_SCOPE = google_scopes.CONTACTS_WRITE_SCOPE
    DRIVE_READ_SCOPES = google_scopes.DRIVE_READ_SCOPES
    DRIVE_FILE_SCOPES = google_scopes.DRIVE_FILE_SCOPES
    DRIVE_WRITE_SCOPES = google_scopes.DRIVE_WRITE_SCOPES
    DRIVE_CONTRACTS_READ_SCOPES = google_scopes.DRIVE_CONTRACTS_READ_SCOPES
    GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
    GMAIL_DRAFT_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
    GMAIL_FULL_SCOPE = "https://mail.google.com/"

    def __init__(self, db: Session):
        self.db = db
        self.repository = UserConnectionRepository(db)
        self.credential_repository = OAuthCredentialRepository(db)
        self.oauth_state_service = OAuthStateService(db)

    def connect_account(
        self,
        user_id: UUID,
        provider: str,
        provider_account_id: str,
        email: str | None = None,
    ) -> dict:
        """
        Create a new connection or update an existing one.

        Duplicate accounts for the same user and provider
        reuse the existing connection.
        """

        normalized_provider = provider.strip().lower()

        if not normalized_provider:
            raise ValueError("Provider cannot be empty.")

        if not provider_account_id or not provider_account_id.strip():
            raise ValueError("Provider account ID cannot be empty.")

        if email is not None:
            email = email.strip().lower() or None

        try:
            connection = self.repository.get_by_provider_account(
                user_id=user_id,
                provider=normalized_provider,
                provider_account_id=provider_account_id.strip(),
            )

            if connection:
                connection = self.repository.update_connection(
                    connection=connection,
                    email=email,
                )
                action = "reconnected"

            else:
                connection = self.repository.create(
                    user_id=user_id,
                    provider=normalized_provider,
                    provider_account_id=provider_account_id.strip(),
                    email=email,
                )
                action = "created"

            self.db.flush()

            return {
                "id": connection.id,
                "user_id": connection.user_id,
                "provider": connection.provider,
                "provider_account_id": connection.provider_account_id,
                "email": connection.email,
                "status": connection.status,
                "connected_at": connection.connected_at,
                "action": action,
            }

        except Exception:
            self.db.rollback()
            raise


    def list_connections(
        self,
        user_id: UUID,
    ) -> list[dict]:

        connections = self.repository.list_by_user(
            user_id=user_id,
        )

        results = []
        for connection in connections:
            credential = (
                self.credential_repository.get_by_connection(
                    user_id=user_id,
                    connection_id=connection.id,
                )
                if connection.provider == "google"
                and connection.status == "connected"
                else None
            )
            granted_scopes = set(credential.scopes or []) if credential else set()
            calendar_events_read = bool(
                granted_scopes & self.CALENDAR_EVENT_READ_SCOPES
            )
            calendar_list_read = bool(
                granted_scopes & self.CALENDAR_LIST_READ_SCOPES
            )
            calendar_freebusy = bool(
                granted_scopes & self.CALENDAR_FREEBUSY_SCOPES
            )
            calendar_connected = calendar_events_read or calendar_freebusy
            calendar_api_available = calendar_events_read or calendar_freebusy
            calendar_api_complete = (
                calendar_events_read and calendar_freebusy
            )
            drive_read = bool(granted_scopes & self.DRIVE_READ_SCOPES)
            drive_file = bool(granted_scopes & self.DRIVE_FILE_SCOPES)
            drive_write = bool(granted_scopes & self.DRIVE_WRITE_SCOPES)
            drive_contracts_read = bool(
                granted_scopes & self.DRIVE_CONTRACTS_READ_SCOPES
            )

            results.append({
                "id": connection.id,
                "provider": connection.provider,
                "email": connection.email,
                "status": connection.status,
                "connected_at": connection.connected_at,
                "created_at": connection.created_at,
                "capabilities": {
                    "gmail": self.GMAIL_READ_SCOPE in granted_scopes,
                    "gmail_draft": (
                        self.GMAIL_DRAFT_SCOPE in granted_scopes
                        or self.GMAIL_FULL_SCOPE in granted_scopes
                    ),
                    "gmail_send": (
                        self.GMAIL_SEND_SCOPE in granted_scopes
                        or self.GMAIL_FULL_SCOPE in granted_scopes
                    ),
                    "calendar": calendar_connected,
                    "calendar_events_read": calendar_events_read,
                    "calendar_list_read": calendar_list_read,
                    "calendar_freebusy": calendar_freebusy,
                    "calendar_api_available": calendar_api_available,
                    "calendar_api_complete": calendar_api_complete,
                    "calendar_chat_available": calendar_api_available,
                    "calendar_write_permission_granted": (
                        self.CALENDAR_EVENTS_WRITE_SCOPE in granted_scopes
                    ),
                    "calendar_create_feature_enabled": (
                        settings.CALENDAR_CREATE_EVENT_ENABLED
                    ),
                    "calendar_update_feature_enabled": (
                        settings.CALENDAR_UPDATE_EVENT_ENABLED
                    ),
                    "calendar_delete_feature_enabled": (
                        settings.CALENDAR_DELETE_EVENT_ENABLED
                    ),
                    "calendar_create_execution_available": (
                        self.CALENDAR_EVENTS_WRITE_SCOPE in granted_scopes
                        and settings.CALENDAR_CREATE_EVENT_ENABLED
                    ),
                    "calendar_update_execution_available": (
                        self.CALENDAR_EVENTS_WRITE_SCOPE in granted_scopes
                        and settings.CALENDAR_UPDATE_EVENT_ENABLED
                    ),
                    "calendar_delete_execution_available": (
                        self.CALENDAR_EVENTS_WRITE_SCOPE in granted_scopes
                        and settings.CALENDAR_DELETE_EVENT_ENABLED
                    ),
                    "calendar_write_execution_available": (
                        self.CALENDAR_EVENTS_WRITE_SCOPE in granted_scopes
                        and any((
                            settings.CALENDAR_CREATE_EVENT_ENABLED,
                            settings.CALENDAR_UPDATE_EVENT_ENABLED,
                            settings.CALENDAR_DELETE_EVENT_ENABLED,
                        ))
                    ),
                    "calendar_write_available": (
                        self.CALENDAR_EVENTS_WRITE_SCOPE in granted_scopes
                        and any((
                            settings.CALENDAR_CREATE_EVENT_ENABLED,
                            settings.CALENDAR_UPDATE_EVENT_ENABLED,
                            settings.CALENDAR_DELETE_EVENT_ENABLED,
                        ))
                    ),
                    "contacts": self.CONTACTS_READ_SCOPE in granted_scopes
                    or self.CONTACTS_WRITE_SCOPE in granted_scopes,
                    "contacts_read": (
                        self.CONTACTS_READ_SCOPE in granted_scopes
                        or self.CONTACTS_WRITE_SCOPE in granted_scopes
                    ),
                    "contacts_write_permission_granted": (
                        self.CONTACTS_WRITE_SCOPE in granted_scopes
                    ),
                    "contacts_create_feature_enabled": (
                        settings.CONTACTS_CREATE_ENABLED
                    ),
                    "contacts_update_feature_enabled": (
                        settings.CONTACTS_UPDATE_ENABLED
                    ),
                    "contacts_delete_feature_enabled": (
                        settings.CONTACTS_DELETE_ENABLED
                    ),
                    "contacts_create_execution_available": (
                        self.CONTACTS_WRITE_SCOPE in granted_scopes
                        and settings.CONTACTS_CREATE_ENABLED
                    ),
                    "contacts_update_execution_available": (
                        self.CONTACTS_WRITE_SCOPE in granted_scopes
                        and settings.CONTACTS_UPDATE_ENABLED
                    ),
                    "contacts_delete_execution_available": (
                        self.CONTACTS_WRITE_SCOPE in granted_scopes
                        and settings.CONTACTS_DELETE_ENABLED
                    ),
                    "contacts_write_execution_available": (
                        self.CONTACTS_WRITE_SCOPE in granted_scopes
                        and any((
                            settings.CONTACTS_CREATE_ENABLED,
                            settings.CONTACTS_UPDATE_ENABLED,
                            settings.CONTACTS_DELETE_ENABLED,
                        ))
                    ),
                    # Google Drive is temporarily disabled for production readiness.
                    # Existing stored grants must not activate Drive functionality.
                    "drive": False,
                    "drive_read": False,
                    "drive_file": False,
                    "drive_contracts_read": False,
                    "drive_write_permission_granted": False,
                },
            })

        return results

    def get_user_calendar_capabilities(
        self,
        user_id: UUID,
    ) -> dict[str, bool]:
        """Aggregate Calendar grants across only this user's connections."""
        return self.aggregate_calendar_capabilities(
            self.list_connections(user_id=user_id)
        )

    @staticmethod
    def aggregate_calendar_capabilities(
        connections: list[dict],
    ) -> dict[str, bool]:
        """Aggregate already ownership-scoped connection summaries."""
        google_connections = [
            connection
            for connection in connections
            if connection["provider"] == "google"
            and connection["status"] == "connected"
        ]
        capabilities = [
            connection["capabilities"]
            for connection in google_connections
        ]
        events_read = any(
            item["calendar_events_read"] for item in capabilities
        )
        freebusy = any(
            item["calendar_freebusy"] for item in capabilities
        )
        connected = any(item["calendar"] for item in capabilities)
        write_permission_granted = any(
            item.get("calendar_write_permission_granted", False)
            for item in capabilities
        )
        return {
            "connected": connected,
            "events_read": events_read,
            "freebusy": freebusy,
            "api_available": events_read or freebusy,
            "api_complete": events_read and freebusy,
            "chat_available": events_read or freebusy,
            "google_connection_available": bool(google_connections),
            "permissions_need_consent": bool(google_connections)
            and not (events_read and freebusy),
            "write_permission_granted": write_permission_granted,
            "create_feature_enabled": (
                settings.CALENDAR_CREATE_EVENT_ENABLED
            ),
            "update_feature_enabled": (
                settings.CALENDAR_UPDATE_EVENT_ENABLED
            ),
            "delete_feature_enabled": (
                settings.CALENDAR_DELETE_EVENT_ENABLED
            ),
            "create_execution_available": any(
                item.get("calendar_create_execution_available", False)
                for item in capabilities
            ),
            "update_execution_available": any(
                item.get("calendar_update_execution_available", False)
                for item in capabilities
            ),
            "delete_execution_available": any(
                item.get("calendar_delete_execution_available", False)
                for item in capabilities
            ),
            "write_execution_available": any(
                item.get("calendar_write_execution_available", False)
                for item in capabilities
            ),
            "write_available": any(
                item.get("calendar_write_execution_available", False)
                for item in capabilities
            ),
        }

    @staticmethod
    def aggregate_contacts_capabilities(
        connections: list[dict],
    ) -> dict[str, bool]:
        """Aggregate Contacts grants across owned Google connections."""
        google_connections = [
            connection
            for connection in connections
            if connection["provider"] == "google"
            and connection["status"] == "connected"
        ]
        capabilities = [
            connection["capabilities"]
            for connection in google_connections
        ]
        connected = any(item.get("contacts") for item in capabilities)
        read_available = any(
            item.get("contacts_read") for item in capabilities
        )
        write_granted = any(
            item.get("contacts_write_permission_granted")
            for item in capabilities
        )
        return {
            "connected": connected,
            "read": read_available,
            "write_permission_granted": write_granted,
            "google_connection_available": bool(google_connections),
            "permissions_need_consent": (
                bool(google_connections) and not read_available
            ),
            "create_feature_enabled": settings.CONTACTS_CREATE_ENABLED,
            "update_feature_enabled": settings.CONTACTS_UPDATE_ENABLED,
            "delete_feature_enabled": settings.CONTACTS_DELETE_ENABLED,
            "create_execution_available": any(
                item.get("contacts_create_execution_available")
                for item in capabilities
            ),
            "update_execution_available": any(
                item.get("contacts_update_execution_available")
                for item in capabilities
            ),
            "delete_execution_available": any(
                item.get("contacts_delete_execution_available")
                for item in capabilities
            ),
            "write_execution_available": any(
                item.get("contacts_write_execution_available")
                for item in capabilities
            ),
        }

    @staticmethod
    def aggregate_drive_capabilities(
        connections: list[dict],
    ) -> dict[str, bool]:
        """Aggregate Drive grants across owned Google connections.

        Google Drive is temporarily disabled for production readiness. Existing
        stored grants/credentials must not activate Drive functionality.
        """
        return {
            "connected": False,
            "read": False,
            "contracts_read": False,
            "file_scope_granted": False,
            "write_permission_granted": False,
            "google_connection_available": False,
            "permissions_need_consent": False,
            "integration_enabled": False,
            "temporarily_unavailable": True,
        }

    def get_user_drive_access(
        self,
        user_id: UUID,
        default_connection_id: str | UUID | None = None,
    ) -> dict[str, object]:
        """Resolve one Drive account for selected-contract reads.

        Google Drive is temporarily disabled for production readiness. Existing
        stored grants/credentials/connections must not activate Drive functionality.
        """
        return {
            "google_connection_available": False,
            "capabilities": self.aggregate_drive_capabilities([]),
            "connection_id": None,
            "selected_connection_email": None,
            "default_connection_invalid": False,
            "default_connection_selected": False,
            "selection_required": False,
            "candidate_emails": [],
            "read_scope_missing": False,
            "enabled": False,
            "temporarily_unavailable": True,
        }

    def get_user_contacts_access(
        self,
        user_id: UUID,
        default_connection_id: str | UUID | None = None,
    ) -> dict[str, object]:
        """Resolve a single Contacts account without Calendar fallback.

        Lookup and write proposals intentionally share the same selected
        account. Multiple eligible accounts are never chosen arbitrarily.
        """
        connections = [
            connection
            for connection in self.list_connections(user_id=user_id)
            if connection["provider"] == "google"
            and connection["status"] == "connected"
        ]
        eligible_connections = [
            connection
            for connection in connections
            if connection["capabilities"].get("contacts_read")
            or connection["capabilities"].get(
                "contacts_write_permission_granted"
            )
        ]

        selected_connection = None
        preference_invalid = False
        if default_connection_id:
            try:
                normalized_default_id = UUID(str(default_connection_id))
            except (TypeError, ValueError):
                normalized_default_id = None
                preference_invalid = True
            if normalized_default_id is not None:
                selected_connection = next(
                    (
                        connection for connection in connections
                        if connection["id"] == normalized_default_id
                    ),
                    None,
                )
                preference_invalid = selected_connection is None
        elif len(eligible_connections) == 1:
            selected_connection = eligible_connections[0]

        read_connection = (
            selected_connection
            if selected_connection
            and selected_connection["capabilities"].get("contacts_read")
            else None
        )
        write_connection = (
            selected_connection
            if selected_connection
            and selected_connection["capabilities"].get(
                "contacts_write_permission_granted"
            )
            else None
        )
        selection_required = (
            not default_connection_id
            and len(eligible_connections) > 1
        )

        return {
            "google_connection_available": bool(connections),
            "capabilities": self.aggregate_contacts_capabilities(
                connections
            ),
            "read_connection_id": (
                str(read_connection["id"]) if read_connection else None
            ),
            "write_connection_id": (
                str(write_connection["id"]) if write_connection else None
            ),
            "selected_connection_email": (
                selected_connection["email"]
                if selected_connection
                else None
            ),
            "default_connection_invalid": preference_invalid,
            "default_connection_selected": bool(default_connection_id),
            "selection_required": selection_required,
            "candidate_emails": [
                connection["email"]
                for connection in eligible_connections
            ],
            "read_scope_missing": (
                bool(selected_connection) and read_connection is None
            ),
            "write_scope_missing": (
                bool(selected_connection) and write_connection is None
            ),
            "create_feature_enabled": (
                settings.CONTACTS_CREATE_ENABLED
            ),
            "update_feature_enabled": (
                settings.CONTACTS_UPDATE_ENABLED
            ),
            "delete_feature_enabled": (
                settings.CONTACTS_DELETE_ENABLED
            ),
            "create_execution_available": (
                bool(write_connection)
                and settings.CONTACTS_CREATE_ENABLED
            ),
            "update_execution_available": (
                bool(write_connection)
                and settings.CONTACTS_UPDATE_ENABLED
            ),
            "delete_execution_available": (
                bool(write_connection)
                and settings.CONTACTS_DELETE_ENABLED
            ),
            "write_execution_available": (
                bool(write_connection)
                and any((
                    settings.CONTACTS_CREATE_ENABLED,
                    settings.CONTACTS_UPDATE_ENABLED,
                    settings.CONTACTS_DELETE_ENABLED,
                ))
            ),
        }

    def get_user_calendar_access(
        self,
        user_id: UUID,
        default_connection_id: str | UUID | None = None,
    ) -> dict[str, object]:
        """Resolve operation connections without crossing user ownership."""
        connections = [
            connection
            for connection in self.list_connections(user_id=user_id)
            if connection["provider"] == "google"
            and connection["status"] == "connected"
        ]
        connection_capabilities = [
            item["capabilities"] for item in connections
        ]
        event_candidates = [
            connection
            for connection in connections
            if connection["capabilities"]["calendar_events_read"]
        ]
        freebusy_candidates = [
            connection
            for connection in connections
            if connection["capabilities"]["calendar_freebusy"]
        ]
        write_candidates = [
            connection
            for connection in connections
            if connection["capabilities"].get(
                "calendar_write_permission_granted",
                False,
            )
        ]

        selected_connection = None
        preference_invalid = False
        if default_connection_id:
            try:
                normalized_default_id = UUID(str(default_connection_id))
            except (TypeError, ValueError):
                normalized_default_id = None
                preference_invalid = True
            if normalized_default_id is not None:
                selected_connection = next(
                    (
                        connection for connection in connections
                        if connection["id"] == normalized_default_id
                    ),
                    None,
                )
                preference_invalid = selected_connection is None

            event_connection = (
                selected_connection
                if selected_connection
                and selected_connection["capabilities"][
                    "calendar_events_read"
                ]
                else None
            )
            freebusy_connection = (
                selected_connection
                if selected_connection
                and selected_connection["capabilities"]["calendar_freebusy"]
                else None
            )
        else:
            event_connection = (
                event_candidates[0] if len(event_candidates) == 1 else None
            )
            freebusy_connection = (
                freebusy_candidates[0]
                if len(freebusy_candidates) == 1
                else None
            )

        write_connection = (
            selected_connection
            if selected_connection
            and selected_connection["capabilities"].get(
                "calendar_write_permission_granted",
                False,
            )
            else (
                write_candidates[0]
                if not default_connection_id and len(write_candidates) == 1
                else None
            )
        )

        capabilities = {
            "connected": any(
                item["calendar"] for item in connection_capabilities
            ),
            "events_read": any(
                item["calendar_events_read"]
                for item in connection_capabilities
            ),
            "freebusy": any(
                item["calendar_freebusy"]
                for item in connection_capabilities
            ),
            "api_available": any(
                item["calendar_api_available"]
                for item in connection_capabilities
            ),
            "api_complete": (
                any(item["calendar_events_read"] for item in connection_capabilities)
                and any(item["calendar_freebusy"] for item in connection_capabilities)
            ),
            "write_permission_granted": any(
                item["capabilities"].get(
                    "calendar_write_permission_granted",
                    False,
                )
                for item in connections
            ),
            "write_execution_available": (
                bool(write_connection)
                and any((
                    settings.CALENDAR_CREATE_EVENT_ENABLED,
                    settings.CALENDAR_UPDATE_EVENT_ENABLED,
                    settings.CALENDAR_DELETE_EVENT_ENABLED,
                ))
            ),
        }
        return {
            "google_connection_available": bool(connections),
            "capabilities": capabilities,
            "event_read_connection_id": (
                str(event_connection["id"]) if event_connection else None
            ),
            "freebusy_connection_id": (
                str(freebusy_connection["id"]) if freebusy_connection else None
            ),
            "default_connection_invalid": preference_invalid,
            "default_connection_selected": bool(default_connection_id),
            "selected_connection_email": (
                selected_connection["email"] if selected_connection else None
            ),
            "event_selection_required": (
                not default_connection_id and len(event_candidates) > 1
            ),
            "freebusy_selection_required": (
                not default_connection_id and len(freebusy_candidates) > 1
            ),
            "event_candidate_emails": [
                connection["email"] for connection in event_candidates
            ],
            "freebusy_candidate_emails": [
                connection["email"] for connection in freebusy_candidates
            ],
            "selected_event_scope_missing": (
                bool(selected_connection)
                and not selected_connection["capabilities"][
                    "calendar_events_read"
                ]
            ),
            "selected_freebusy_scope_missing": (
                bool(selected_connection)
                and not selected_connection["capabilities"]["calendar_freebusy"]
            ),
            "calendar_write_available": (
                bool(write_connection)
                and any((
                    settings.CALENDAR_CREATE_EVENT_ENABLED,
                    settings.CALENDAR_UPDATE_EVENT_ENABLED,
                    settings.CALENDAR_DELETE_EVENT_ENABLED,
                ))
            ),
            "calendar_write_permission_granted": (
                bool(write_connection)
            ),
            "calendar_create_feature_enabled": (
                settings.CALENDAR_CREATE_EVENT_ENABLED
            ),
            "calendar_update_feature_enabled": (
                settings.CALENDAR_UPDATE_EVENT_ENABLED
            ),
            "calendar_delete_feature_enabled": (
                settings.CALENDAR_DELETE_EVENT_ENABLED
            ),
            "calendar_create_execution_available": (
                bool(write_connection)
                and settings.CALENDAR_CREATE_EVENT_ENABLED
            ),
            "calendar_update_execution_available": (
                bool(write_connection)
                and settings.CALENDAR_UPDATE_EVENT_ENABLED
            ),
            "calendar_delete_execution_available": (
                bool(write_connection)
                and settings.CALENDAR_DELETE_EVENT_ENABLED
            ),
            "calendar_write_execution_available": (
                bool(write_connection)
                and any((
                    settings.CALENDAR_CREATE_EVENT_ENABLED,
                    settings.CALENDAR_UPDATE_EVENT_ENABLED,
                    settings.CALENDAR_DELETE_EVENT_ENABLED,
                ))
            ),
            "calendar_write_create_enabled": (
                bool(write_connection)
                and settings.CALENDAR_CREATE_EVENT_ENABLED
            ),
            "calendar_write_update_enabled": (
                bool(write_connection)
                and settings.CALENDAR_UPDATE_EVENT_ENABLED
            ),
            "calendar_write_delete_enabled": (
                bool(write_connection)
                and settings.CALENDAR_DELETE_EVENT_ENABLED
            ),
            "calendar_write_selection_required": (
                not default_connection_id and len(write_candidates) > 1
            ),
            "calendar_selected_write_scope_missing": (
                bool(selected_connection)
                and not selected_connection["capabilities"].get(
                    "calendar_write_permission_granted",
                    False,
                )
            ),
            "write_connection_id": (
                str(write_connection["id"]) if write_connection else None
            ),
            "write_connection_email": (
                write_connection["email"] if write_connection else None
            ),
        }

    def get_granted_google_scopes(
        self,
        user_id: UUID,
        connection_id: UUID,
    ) -> set[str] | None:
        """Return grants only for an active Google connection owned by user."""
        connection = self.repository.get_active_google_connection(
            user_id=user_id,
            connection_id=connection_id,
        )
        if connection is None:
            return None

        credential = self.credential_repository.get_by_connection(
            user_id=user_id,
            connection_id=connection_id,
        )
        if credential is None:
            return None

        return set(credential.scopes or [])

    def disconnect_account(
        self,
        user_id: UUID,
        connection_id: UUID,
    ) -> bool:

        try:
            connection = self.repository.get_by_id(
                user_id=user_id,
                connection_id=connection_id,
            )
            if connection is None:
                self.db.rollback()
                return False

            if connection.provider == "google":
                # OAuth callbacks lock their consumed state before saving
                # credentials. Delete states first to prevent an in-flight
                # callback from recreating a just-disconnected connection.
                self.oauth_state_service.invalidate_user_states(
                    user_id=user_id,
                    provider="google",
                )

            connection = self.repository.get_by_id_for_update(
                user_id=user_id,
                connection_id=connection_id,
            )
            if connection is None:
                self.db.rollback()
                return False

            if connection.provider == "google":
                self.credential_repository.delete(
                    user_id=user_id,
                    connection_id=connection_id,
                )

            self.db.delete(connection)
            self.db.flush()

            self.db.commit()

            return True

        except Exception:
            self.db.rollback()
            raise