from uuid import UUID
from typing import Any

from sqlalchemy.orm import Session

from app.core.security import encrypt_data, decrypt_data
from app.repositories.oauth_credential_repository import (
    OAuthCredentialRepository,
)
from datetime import datetime, timedelta, timezone

from app.integrations.google.oauth_client import GoogleOAuthClient

class OAuthCredentialService:
    """
    Handles OAuth credential encryption, decryption and persistence.

    Database transaction management remains the responsibility
    of the caller/service layer.
    """

    def __init__(self, db: Session):
        self.repository = OAuthCredentialRepository(db)

    def create_credentials(
        self,
        user_id: UUID,
        connection_id: UUID,
        access_token: str,
        refresh_token: str | None = None,
        token_type: str = "Bearer",
        expires_at: datetime | None = None,
        scopes: list[str] | None = None,
    ) -> dict[str, Any]:
        """Encrypt and persist OAuth credentials."""

        if not access_token or not access_token.strip():
            raise ValueError("Access token cannot be empty.")

        encrypted_access_token = encrypt_data(access_token)

        encrypted_refresh_token = (
            encrypt_data(refresh_token)
            if refresh_token
            else None
        )

        credential = self.repository.create(
            user_id=user_id,
            connection_id=connection_id,
            access_token_encrypted=encrypted_access_token,
            refresh_token_encrypted=encrypted_refresh_token,
            token_type=token_type,
            expires_at=expires_at,
            scopes=scopes or [],
        )

        return self._credential_metadata(credential)

    def get_credentials(
        self,
        user_id: UUID,
        connection_id: UUID,
    ) -> dict[str, Any] | None:
        """Retrieve and decrypt credentials for the specified user."""

        credential = self.repository.get_by_connection(
            user_id=user_id,
            connection_id=connection_id,
        )

        if credential is None:
            return None

        access_token = decrypt_data(
            credential.access_token_encrypted
        )

        refresh_token = (
            decrypt_data(credential.refresh_token_encrypted)
            if credential.refresh_token_encrypted
            else None
        )

        return {
            "connection_id": str(credential.connection_id),
            "user_id": str(credential.user_id),
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": credential.token_type,
            "expires_at": credential.expires_at,
            "scopes": credential.scopes,
        }
    

    def save_credentials(
        self,
        user_id: UUID,
        connection_id: UUID,
        access_token: str,
        refresh_token: str | None = None,
        token_type: str = "Bearer",
        expires_at: datetime | None = None,
        scopes: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Create credentials for a new connection or update
        existing credentials during account reconnection.

        Transaction management remains with the caller.
        """

        existing = self.repository.get_by_connection(
            user_id=user_id,
            connection_id=connection_id,
        )

        if existing is None:
            return self.create_credentials(
                user_id=user_id,
                connection_id=connection_id,
                access_token=access_token,
                refresh_token=refresh_token,
                token_type=token_type,
                expires_at=expires_at,
                scopes=scopes,
            )

        updated = self.update_credentials(
            user_id=user_id,
            connection_id=connection_id,
            access_token=access_token,
            refresh_token=refresh_token,
            token_type=token_type,
            expires_at=expires_at,
            scopes=scopes,
        )

        if updated is None:
            raise RuntimeError(
                "Unable to update existing OAuth credentials."
            )

        return updated

    def update_credentials(
        self,
        user_id: UUID,
        connection_id: UUID,
        access_token: str,
        refresh_token: str | None = None,
        token_type: str = "Bearer",
        expires_at: datetime | None = None,
        scopes: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """
        Encrypt and update tokens.

        If the OAuth provider does not return a new refresh token,
        preserve the previously stored encrypted refresh token.
        """

        if not access_token or not access_token.strip():
            raise ValueError("Access token cannot be empty.")

        existing = self.repository.get_by_connection(
            user_id=user_id,
            connection_id=connection_id,
        )

        if existing is None:
            return None

        encrypted_access_token = encrypt_data(access_token)

        encrypted_refresh_token = (
            encrypt_data(refresh_token)
            if refresh_token
            else existing.refresh_token_encrypted
        )

        credential = self.repository.update_tokens(
            user_id=user_id,
            connection_id=connection_id,
            access_token_encrypted=encrypted_access_token,
            refresh_token_encrypted=encrypted_refresh_token,
            token_type=token_type,
            expires_at=expires_at,
            scopes=scopes if scopes is not None else existing.scopes,
        )

        if credential is None:
            return None

        return self._credential_metadata(credential)

    def delete_credentials(
        self,
        user_id: UUID,
        connection_id: UUID,
    ) -> bool:
        """Delete credentials belonging to the specified user."""

        return self.repository.delete(
            user_id=user_id,
            connection_id=connection_id,
        )

    @staticmethod
    def _credential_metadata(
        credential: Any,
    ) -> dict[str, Any]:
        """Return safe metadata without exposing encrypted tokens."""

        return {
            "connection_id": str(credential.connection_id),
            "user_id": str(credential.user_id),
            "token_type": credential.token_type,
            "expires_at": credential.expires_at,
            "scopes": credential.scopes,
        }
    

    async def get_valid_access_token(
        self,
        user_id: UUID,
        connection_id: UUID,
        rejected_access_token: str | None = None,
    ) -> str:
        """
        Return a valid Google access token.

        If an API request rejected a token with 401:
        - Check whether another request already refreshed it.
        - Reuse the newer token if available.
        - Otherwise force a refresh.

        The caller owns transaction management.
        """

        credential = self.repository.get_by_connection_for_update(
            user_id=user_id,
            connection_id=connection_id,
        )

        if credential is None:
            raise ValueError("OAuth credentials not found.")

        access_token = decrypt_data(
            credential.access_token_encrypted
        )

        refresh_token = (
            decrypt_data(credential.refresh_token_encrypted)
            if credential.refresh_token_encrypted
            else None
        )

        now = datetime.now(timezone.utc)
        refresh_threshold = now + timedelta(minutes=5)

        token_is_valid = (
            credential.expires_at is not None
            and credential.expires_at > refresh_threshold
        )

        # A 401 was received for a previous token.
        if rejected_access_token is not None:

            # Another request may already have refreshed it.
            if (
                access_token != rejected_access_token
                and token_is_valid
            ):
                return access_token

        # Normal request: existing token is still valid.
        elif token_is_valid:
            return access_token

        if not refresh_token:
            raise ValueError(
                "Refresh token unavailable. Reconnection required."
            )

        oauth_client = GoogleOAuthClient()

        token_data = await oauth_client.refresh_access_token(
            refresh_token=refresh_token,
        )

        new_access_token = token_data["access_token"]

        new_expires_at = now + timedelta(
            seconds=token_data["expires_in"]
        )

        updated_credentials = self.update_credentials(
            user_id=user_id,
            connection_id=connection_id,
            access_token=new_access_token,
            refresh_token=token_data.get("refresh_token"),
            token_type=token_data.get(
                "token_type",
                credential.token_type,
            ),
            expires_at=new_expires_at,
        )

        if updated_credentials is None:
            raise RuntimeError(
                "Unable to persist refreshed Google credentials."
            )

        return new_access_token