from uuid import UUID

from sqlalchemy.orm import Session

from app.repositories.user_connection_repository import (
    UserConnectionRepository,
)


class UserConnectionService:
    """
    Business logic for external account connections.

    Handles connection creation and reconnection.
    """

    def __init__(self, db: Session):
        self.db = db
        self.repository = UserConnectionRepository(db)

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