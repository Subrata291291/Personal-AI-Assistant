
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.oauth_credential import OAuthCredential
from app.db.models.user_connection import UserConnection


class OAuthCredentialRepository:
    """
    Repository for user-specific encrypted OAuth credentials.

    Transaction commits and rollbacks are managed by the service layer.
    """

    def __init__(self, db: Session):
        self.db = db

    def create(
        self,
        user_id: UUID,
        connection_id: UUID,
        access_token_encrypted: str,
        refresh_token_encrypted: str | None = None,
        token_type: str = "Bearer",
        expires_at: datetime | None = None,
        scopes: list[str] | None = None,
    ) -> OAuthCredential:
        """
        Create encrypted OAuth credentials for an existing user connection.
        """

        # Verify that this connection belongs to the requested user.
        connection = self.db.scalar(
            select(UserConnection.id).where(
                UserConnection.id == connection_id,
                UserConnection.user_id == user_id,
            )
        )

        if connection is None:
            raise ValueError(
                "Connection does not exist or does not belong to this user."
            )

        credential = OAuthCredential(
            connection_id=connection_id,
            user_id=user_id,
            access_token_encrypted=access_token_encrypted,
            refresh_token_encrypted=refresh_token_encrypted,
            token_type=token_type,
            expires_at=expires_at,
            scopes=scopes or [],
        )

        self.db.add(credential)
        self.db.flush()

        return credential

    def get_by_connection(
        self,
        user_id: UUID,
        connection_id: UUID,
    ) -> OAuthCredential | None:
        """
        Retrieve credentials only when both user_id and connection_id match.
        """

        statement = select(OAuthCredential).where(
            OAuthCredential.user_id == user_id,
            OAuthCredential.connection_id == connection_id,
        )

        return self.db.scalar(statement)

    def update_tokens(
        self,
        user_id: UUID,
        connection_id: UUID,
        access_token_encrypted: str,
        refresh_token_encrypted: str | None,
        token_type: str,
        expires_at: datetime | None,
        scopes: list[str],
    ) -> OAuthCredential | None:
        """
        Update encrypted tokens belonging to the specified user connection.
        """

        credential = self.get_by_connection(
            user_id=user_id,
            connection_id=connection_id,
        )

        if credential is None:
            return None

        credential.access_token_encrypted = access_token_encrypted
        credential.refresh_token_encrypted = refresh_token_encrypted
        credential.token_type = token_type
        credential.expires_at = expires_at
        credential.scopes = scopes

        self.db.flush()

        return credential

    def delete(
        self,
        user_id: UUID,
        connection_id: UUID,
    ) -> bool:
        """
        Delete credentials only when both ownership identifiers match.
        """

        credential = self.get_by_connection(
            user_id=user_id,
            connection_id=connection_id,
        )

        if credential is None:
            return False

        self.db.delete(credential)
        self.db.flush()

        return True
