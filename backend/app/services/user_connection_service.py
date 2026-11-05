from uuid import UUID

from sqlalchemy.orm import Session

from app.integrations.google import scopes as google_scopes
from app.repositories.oauth_credential_repository import (
    OAuthCredentialRepository,
)
from app.repositories.user_connection_repository import (
    UserConnectionRepository,
)


class UserConnectionService:
    """
    Business logic for external account connections.

    Handles connection creation and reconnection.
    """

    GMAIL_READ_SCOPE = (
        "https://www.googleapis.com/auth/gmail.readonly"
    )
    CALENDAR_EVENTS_READ_SCOPE = google_scopes.CALENDAR_EVENTS_READ_SCOPE
    CALENDAR_FREEBUSY_SCOPE = google_scopes.CALENDAR_FREEBUSY_SCOPE
    CALENDAR_EVENT_READ_SCOPES = google_scopes.CALENDAR_EVENT_READ_SCOPES
    CALENDAR_FREEBUSY_SCOPES = google_scopes.CALENDAR_FREEBUSY_SCOPES
    GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
    GMAIL_DRAFT_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
    GMAIL_FULL_SCOPE = "https://mail.google.com/"

    def __init__(self, db: Session):
        self.db = db
        self.repository = UserConnectionRepository(db)
        self.credential_repository = OAuthCredentialRepository(db)

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
            calendar_freebusy = bool(
                granted_scopes & self.CALENDAR_FREEBUSY_SCOPES
            )
            calendar_connected = calendar_events_read or calendar_freebusy
            calendar_api_available = calendar_events_read or calendar_freebusy
            calendar_api_complete = (
                calendar_events_read and calendar_freebusy
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
                    "calendar_freebusy": calendar_freebusy,
                    "calendar_api_available": calendar_api_available,
                    "calendar_api_complete": calendar_api_complete,
                    "calendar_chat_available": calendar_api_available,
                    "calendar_write_available": False,
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
            "write_available": False,
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
            "calendar_write_available": False,
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
            deleted = self.repository.delete_by_id(
                user_id=user_id,
                connection_id=connection_id,
            )

            if not deleted:
                return False

            self.db.commit()

            return True

        except Exception:
            self.db.rollback()
            raise