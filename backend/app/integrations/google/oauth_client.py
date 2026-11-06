import re
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx

from app.core.config import settings
from app.integrations.google.exceptions import (
    GoogleOAuthAuthorizationError,
    GoogleOAuthTransientError,
)
from app.integrations.google import scopes as google_scopes


class GoogleOAuthClient:
    """
    Handles Google OAuth authorization and account verification.

    Responsibilities:
    - Build Google authorization URL.
    - Validate OAuth state and PKCE challenge.
    - Exchange authorization code for OAuth tokens.
    - Retrieve Google account identity.

    Token persistence and encryption are handled by
    separate service layers.
    """

    AUTHORIZATION_ENDPOINT = (
        "https://accounts.google.com/o/oauth2/v2/auth"
    )

    TOKEN_ENDPOINT = (
        "https://oauth2.googleapis.com/token"
    )

    USERINFO_ENDPOINT = (
        "https://openidconnect.googleapis.com/v1/userinfo"
    )

    REQUEST_TIMEOUT = 15.0
    IDENTITY_SCOPES = ("openid", "email", "profile")
    CALENDAR_EVENTS_READ_SCOPE = google_scopes.CALENDAR_EVENTS_READ_SCOPE
    CALENDAR_EVENTS_WRITE_SCOPE = google_scopes.CALENDAR_EVENTS_WRITE_SCOPE
    CALENDAR_FREEBUSY_SCOPE = google_scopes.CALENDAR_FREEBUSY_SCOPE

    @staticmethod
    def _validate_redirect_uri(redirect_uri: str) -> None:
        """Validate the configured callback URI."""

        if not redirect_uri:
            raise ValueError(
                "Google redirect URI is not configured."
            )

        parsed = urlsplit(redirect_uri)

        if not parsed.scheme or not parsed.netloc:
            raise ValueError(
                "Invalid Google redirect URI."
            )

        if parsed.username or parsed.password:
            raise ValueError(
                "Redirect URI cannot contain credentials."
            )

        expected_callback_path = (
            f"{settings.API_V1_PREFIX.rstrip('/')}/oauth/google/callback"
        )
        if parsed.path != expected_callback_path:
            raise ValueError(
                "Google redirect URI must target the backend OAuth callback."
            )

        if settings.APP_ENV.lower() == "production":
            if parsed.scheme != "https":
                raise ValueError(
                    "Production Google redirect URI must use HTTPS."
                )
        elif parsed.scheme not in {"http", "https"}:
            raise ValueError(
                "Unsupported redirect URI scheme."
            )

    @staticmethod
    def _validate_state(state: str) -> None:
        """Validate the generated OAuth state format."""

        if not state or len(state) < 32:
            raise ValueError(
                "Invalid OAuth state."
            )

    @staticmethod
    def _validate_code_challenge(
        code_challenge: str,
    ) -> None:
        """Validate an S256 PKCE code challenge."""

        if not re.fullmatch(
            r"[A-Za-z0-9_-]{43}",
            code_challenge or "",
        ):
            raise ValueError(
                "Invalid PKCE code challenge."
            )

    def build_authorization_url(
        self,
        state: str,
        code_challenge: str,
        service: str = "gmail",
    ) -> str:
        """
        Build the Google OAuth authorization URL.

        Uses configured OAuth scopes and offline access
        for refresh-token support.
        """

        if not settings.GOOGLE_CLIENT_ID:
            raise RuntimeError(
                "GOOGLE_CLIENT_ID is not configured."
            )

        if not settings.GOOGLE_REDIRECT_URI:
            raise RuntimeError(
                "GOOGLE_REDIRECT_URI is not configured."
            )

        self._validate_state(state)

        self._validate_code_challenge(
            code_challenge
        )

        self._validate_redirect_uri(
            settings.GOOGLE_REDIRECT_URI
        )

        scopes = self.scopes_for_service(service)

        if not scopes:
            raise RuntimeError(
                "Google OAuth scopes are not configured."
            )

        params = {
            "client_id": settings.GOOGLE_CLIENT_ID,
            "redirect_uri": settings.GOOGLE_REDIRECT_URI,
            "response_type": "code",
            "scope": " ".join(scopes),
            "access_type": settings.GOOGLE_OAUTH_ACCESS_TYPE,
            "prompt": settings.GOOGLE_OAUTH_PROMPT,
            "include_granted_scopes": "true",
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }

        return (
            f"{self.AUTHORIZATION_ENDPOINT}?"
            f"{urlencode(params)}"
        )

    @classmethod
    def scopes_for_service(cls, service: str) -> list[str]:
        """Return the least-privilege scope set for one Google service."""

        if service == "calendar":
            requested_scopes = [
                *cls.IDENTITY_SCOPES,
                cls.CALENDAR_EVENTS_READ_SCOPE,
                cls.CALENDAR_FREEBUSY_SCOPE,
            ]
        elif service == "calendar_write":
            requested_scopes = [
                *cls.IDENTITY_SCOPES,
                cls.CALENDAR_EVENTS_READ_SCOPE,
                cls.CALENDAR_FREEBUSY_SCOPE,
                cls.CALENDAR_EVENTS_WRITE_SCOPE,
            ]
        elif service == "gmail":
            configured_scopes = settings.google_oauth_scopes_list
            gmail_scopes = [
                scope
                for scope in configured_scopes
                if scope.startswith(
                    "https://www.googleapis.com/auth/gmail."
                )
                or scope == "https://mail.google.com/"
            ]
            requested_scopes = [
                *cls.IDENTITY_SCOPES,
                *gmail_scopes,
            ]
        else:
            raise ValueError("Unsupported Google service.")

        return list(dict.fromkeys(requested_scopes))

    async def exchange_authorization_code(
        self,
        code: str,
        code_verifier: str,
        redirect_uri: str,
    ) -> dict[str, Any]:
        """
        Exchange Google authorization code for OAuth tokens.

        Uses the PKCE code verifier and the exact redirect URI
        stored during the authorization flow.
        """

        if not code or not code.strip():
            raise ValueError(
                "Authorization code cannot be empty."
            )

        if not code_verifier or not code_verifier.strip():
            raise ValueError(
                "PKCE code verifier cannot be empty."
            )

        if not settings.GOOGLE_CLIENT_ID:
            raise RuntimeError(
                "Google OAuth client ID is missing."
            )

        if not settings.GOOGLE_CLIENT_SECRET:
            raise RuntimeError(
                "Google OAuth client secret is missing."
            )

        self._validate_redirect_uri(
            redirect_uri
        )

        payload = {
            "code": code,
            "client_id": settings.GOOGLE_CLIENT_ID,
            "client_secret": (
                settings.GOOGLE_CLIENT_SECRET.get_secret_value()
            ),
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
            "code_verifier": code_verifier,
        }

        try:
            async with httpx.AsyncClient(
                timeout=self.REQUEST_TIMEOUT
            ) as client:

                response = await client.post(
                    self.TOKEN_ENDPOINT,
                    data=payload,
                )

                response.raise_for_status()

                token_data = response.json()

        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeError(
                "Google authorization code exchange failed."
            ) from exc

        if not isinstance(token_data, dict):
            raise RuntimeError(
                "Invalid Google token response."
            )

        access_token = token_data.get(
            "access_token"
        )

        if not isinstance(access_token, str) or not access_token:
            raise RuntimeError(
                "Google token response did not contain an access token."
            )

        return token_data

    async def get_user_info(
        self,
        access_token: str,
    ) -> dict[str, str]:
        """
        Retrieve Google account identity using the access token.

        Returns:
        - provider_account_id: Google subject identifier (sub).
        - email: Verified Google account email.
        """

        if not access_token or not access_token.strip():
            raise ValueError(
                "Access token cannot be empty."
            )

        try:
            async with httpx.AsyncClient(
                timeout=self.REQUEST_TIMEOUT
            ) as client:

                response = await client.get(
                    self.USERINFO_ENDPOINT,
                    headers={
                        "Authorization": f"Bearer {access_token}"
                    },
                )

                response.raise_for_status()

                user_data = response.json()

        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeError(
                "Unable to retrieve Google account information."
            ) from exc

        if not isinstance(user_data, dict):
            raise RuntimeError(
                "Invalid Google user information response."
            )

        subject = user_data.get("sub")
        email = user_data.get("email")
        email_verified = user_data.get("email_verified")

        if not isinstance(subject, str) or not subject:
            raise RuntimeError(
                "Google account subject is missing."
            )

        if not isinstance(email, str) or not email:
            raise RuntimeError(
                "Google account email is missing."
            )

        if email_verified is not True:
            raise RuntimeError(
                "Google account email is not verified."
            )

        return {
            "provider_account_id": subject,
            "email": email,
        }


    async def refresh_access_token(
        self,
        refresh_token: str,
    ) -> dict[str, Any]:
        """
        Exchange a Google refresh token for a new access token.

        Google normally does not return a new refresh token
        during this operation. The existing refresh token
        must therefore be preserved.
        """

        if not refresh_token or not refresh_token.strip():
            raise ValueError(
                "Google refresh token cannot be empty."
            )

        if not settings.GOOGLE_CLIENT_ID:
            raise RuntimeError(
                "Google OAuth client ID is missing."
            )

        if not settings.GOOGLE_CLIENT_SECRET:
            raise RuntimeError(
                "Google OAuth client secret is missing."
            )

        payload = {
            "client_id": settings.GOOGLE_CLIENT_ID,
            "client_secret": (
                settings.GOOGLE_CLIENT_SECRET.get_secret_value()
            ),
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }

        try:
            async with httpx.AsyncClient(
                timeout=self.REQUEST_TIMEOUT
            ) as client:

                response = await client.post(
                    self.TOKEN_ENDPOINT,
                    data=payload,
                )

                response.raise_for_status()
                token_data = response.json()

        except httpx.HTTPStatusError as exc:
            error_code = None
            try:
                error_payload = exc.response.json()
            except ValueError:
                error_payload = None
            if isinstance(error_payload, dict):
                error_code = error_payload.get("error")
            if error_code == "invalid_grant":
                raise GoogleOAuthAuthorizationError() from exc
            if (
                exc.response.status_code == 429
                or exc.response.status_code >= 500
            ):
                raise GoogleOAuthTransientError() from exc
            raise RuntimeError(
                "Google access token refresh failed."
            ) from exc

        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise GoogleOAuthTransientError() from exc

        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeError(
                "Google access token refresh failed."
            ) from exc

        if not isinstance(token_data, dict):
            raise RuntimeError(
                "Invalid Google token refresh response."
            )

        access_token = token_data.get("access_token")
        expires_in = token_data.get("expires_in")

        if not isinstance(access_token, str) or not access_token:
            raise RuntimeError(
                "Google refresh response did not contain an access token."
            )

        if (
            isinstance(expires_in, bool)
            or not isinstance(expires_in, (int, float))
            or expires_in <= 0
        ):
            raise RuntimeError(
                "Invalid Google token expiration."
            )

        return token_data
