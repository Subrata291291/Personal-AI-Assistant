from uuid import UUID

from sqlalchemy.orm import Session

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

            results.append({
                "id": connection.id,
                "provider": connection.provider,
                "email": connection.email,
                "status": connection.status,
                "connected_at": connection.connected_at,
                "created_at": connection.created_at,
                "capabilities": {
                    "gmail": self.GMAIL_READ_SCOPE in granted_scopes,
                },
            })

        return results

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