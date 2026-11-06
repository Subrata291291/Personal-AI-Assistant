from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.integrations.google.oauth_client import GoogleOAuthClient
from app.repositories.user_connection_repository import (
    UserConnectionRepository,
)
from app.services.oauth_credential_service import OAuthCredentialService
from app.services.oauth_state_service import OAuthStateService
from app.services.user_connection_service import UserConnectionService


class GoogleOAuthCallbackService:
    """
    Orchestrates the Google OAuth callback workflow.

    Responsibilities:
    - Consume and validate OAuth state.
    - Exchange authorization code for tokens.
    - Verify Google account identity.
    - Create or reconnect the user connection.
    - Encrypt and save OAuth credentials.
    - Commit connection and credentials atomically.

    OAuth state consumption is committed separately before
    token exchange to prevent state replay.
    """

    PROVIDER = "google"

    def __init__(self, db: Session):
        self.db = db

        self.oauth_client = GoogleOAuthClient()
        self.state_service = OAuthStateService(db)
        self.connection_service = UserConnectionService(db)
        self.credential_service = OAuthCredentialService(db)

    async def handle_callback(
        self,
        code: str | None,
        state: str,
        error: str | None = None,
    ) -> dict[str, Any]:
        """
        Process Google's OAuth callback.

        Returns safe connection metadata only.
        Never returns access or refresh tokens.
        """

        if not state or not state.strip():
            raise ValueError("OAuth state is missing.")

        # Consume state before processing the callback.
        # This operation commits independently.
        state_data = self.state_service.consume_state(
            provider=self.PROVIDER,
            state=state,
        )

        if state_data is None:
            raise ValueError(
                "Invalid, expired or already consumed OAuth state."
            )

        # Google may return an error instead of an authorization code.
        if error:
            raise ValueError(
                "Google authorization was not completed."
            )

        if not code or not code.strip():
            raise ValueError(
                "Google authorization code is missing."
            )

        user_id = state_data["user_id"]

        try:
            # Exchange authorization code for OAuth tokens.
            token_data = (
                await self.oauth_client.exchange_authorization_code(
                    code=code,
                    code_verifier=state_data["code_verifier"],
                    redirect_uri=state_data["redirect_uri"],
                )
            )

            access_token = token_data["access_token"]
            refresh_token = token_data.get("refresh_token")
            token_type = token_data.get("token_type", "Bearer")

            expires_in = token_data.get("expires_in")

            if (
                isinstance(expires_in, bool)
                or not isinstance(expires_in, (int, float))
                or expires_in <= 0
            ):
                raise ValueError(
                    "Invalid Google token expiration."
                )

            expires_at = datetime.now(timezone.utc) + timedelta(
                seconds=expires_in
            )

            # Verify the Google account identity.
            google_user = await self.oauth_client.get_user_info(
                access_token=access_token
            )

            # Parse granted scopes returned by Google.
            granted_scope = token_data.get("scope", "")

            if isinstance(granted_scope, str):
                scopes = granted_scope.split()
            else:
                scopes = []

            if not self.state_service.lock_consumed_state_for_callback(
                user_id=user_id,
                provider=self.PROVIDER,
                state=state,
            ):
                raise ValueError(
                    "Google authorization was canceled by disconnect."
                )

            target_connection_id = state_data.get("target_connection_id")
            if state_data.get("service") == "calendar_write":
                if target_connection_id is None:
                    raise ValueError(
                        "Calendar write consent has no selected account."
                    )
                target_connection = UserConnectionRepository(
                    self.db
                ).get_by_id(
                    user_id=user_id,
                    connection_id=target_connection_id,
                )
                if (
                    target_connection is None
                    or target_connection.provider != "google"
                    or target_connection.status != "connected"
                    or target_connection.provider_account_id
                    != google_user["provider_account_id"]
                ):
                    raise ValueError(
                        "The authorized Google account does not match the "
                        "selected connection."
                    )

            # Create or reconnect the user's Google account.
            connection = self.connection_service.connect_account(
                user_id=user_id,
                provider=self.PROVIDER,
                provider_account_id=google_user["provider_account_id"],
                email=google_user["email"],
            )

            # Check whether previously stored credentials contain
            # a refresh token.
            existing_credentials = (
                self.credential_service.get_credentials_for_update(
                    user_id=user_id,
                    connection_id=connection["id"],
                )
            )

            existing_refresh_token = (
                existing_credentials.get("refresh_token")
                if existing_credentials
                else None
            )
            existing_scopes = (
                existing_credentials.get("scopes") or []
                if existing_credentials
                else []
            )

            # Google may omit a refresh token during reconnection.
            # In that case, preserve the previously stored token.
            #
            # For a new connection, a refresh token is mandatory
            # because background access is required.
            if not refresh_token and not existing_refresh_token:
                raise ValueError(
                    "Google did not provide a refresh token. "
                    "Reconnect after revoking the previous Google "
                    "app authorization."
                )

            # Encrypt and save credentials.
            # Existing refresh tokens are preserved if Google
            # does not return a new refresh token.
            self.credential_service.save_credentials(
                user_id=user_id,
                connection_id=connection["id"],
                access_token=access_token,
                refresh_token=refresh_token,
                token_type=token_type,
                expires_at=expires_at,
                scopes=list(dict.fromkeys([*existing_scopes, *scopes])),
            )

            # Both connection and credentials are committed together.
            self.db.commit()

            return {
                "provider": self.PROVIDER,
                "connection_id": str(connection["id"]),
                "email": google_user["email"],
                "status": "connected",
                "action": connection["action"],
            }

        except Exception:
            self.db.rollback()
            raise
