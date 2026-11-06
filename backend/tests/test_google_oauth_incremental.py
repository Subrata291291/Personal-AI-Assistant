import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import pytest

from app.core.config import settings
from app.integrations.google.exceptions import (
    GoogleOAuthAuthorizationError,
    GoogleOAuthTransientError,
)
from app.integrations.google.oauth_client import GoogleOAuthClient
from app.services.google_oauth_callback_service import (
    GoogleOAuthCallbackService,
)
from app.services.oauth_credential_service import OAuthCredentialService
from app.services.user_connection_service import UserConnectionService


@pytest.mark.parametrize(
    ("status_code", "body", "error_type"),
    [
        (400, {"error": "invalid_grant"}, GoogleOAuthAuthorizationError),
        (503, {"error": "temporarily_unavailable"}, GoogleOAuthTransientError),
    ],
)
def test_refresh_token_errors_are_classified_without_exposing_provider_data(
    status_code,
    body,
    error_type,
    monkeypatch,
):
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setattr(
        settings,
        "GOOGLE_CLIENT_SECRET",
        SimpleNamespace(get_secret_value=lambda: "client-secret"),
    )
    request = httpx.Request("POST", GoogleOAuthClient.TOKEN_ENDPOINT)
    response = httpx.Response(
        status_code,
        json=body,
        request=request,
    )

    class FakeAsyncClient:
        def __init__(self, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            return response

    monkeypatch.setattr(
        "app.integrations.google.oauth_client.httpx.AsyncClient",
        FakeAsyncClient,
    )

    with pytest.raises(error_type) as raised:
        asyncio.run(
            GoogleOAuthClient().refresh_access_token("private-refresh-token")
        )

    assert "private-refresh-token" not in str(raised.value)
    assert "client-secret" not in str(raised.value)


@pytest.mark.parametrize(
    ("service", "expected_scopes", "forbidden_scopes"),
    [
        (
            "gmail",
            ["https://www.googleapis.com/auth/gmail.readonly"],
            [
                GoogleOAuthClient.CALENDAR_EVENTS_READ_SCOPE,
                GoogleOAuthClient.CALENDAR_FREEBUSY_SCOPE,
                "https://www.googleapis.com/auth/calendar.events",
            ],
        ),
        (
            "calendar",
            [
                GoogleOAuthClient.CALENDAR_EVENTS_READ_SCOPE,
                GoogleOAuthClient.CALENDAR_FREEBUSY_SCOPE,
            ],
            [
                "https://www.googleapis.com/auth/gmail.readonly",
                "https://www.googleapis.com/auth/calendar.events",
                "https://www.googleapis.com/auth/calendar",
            ],
        ),
    ],
)
def test_google_authorization_requests_scopes_for_only_selected_service(
    service,
    expected_scopes,
    forbidden_scopes,
    monkeypatch,
):
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setattr(
        settings,
        "GOOGLE_REDIRECT_URI",
        "https://example.test/api/v1/oauth/google/callback",
    )
    monkeypatch.setattr(settings, "APP_ENV", "production")
    monkeypatch.setattr(settings, "API_V1_PREFIX", "/api/v1")
    monkeypatch.setattr(settings, "GOOGLE_OAUTH_SCOPES", (
        "openid,email,profile,"
        "https://www.googleapis.com/auth/gmail.readonly,"
        "https://www.googleapis.com/auth/gmail.send,"
        "https://www.googleapis.com/auth/calendar.events"
    ))

    url = GoogleOAuthClient().build_authorization_url(
        state="s" * 43,
        code_challenge="c" * 43,
        service=service,
    )
    params = parse_qs(urlsplit(url).query)
    requested_scopes = params["scope"][0].split()

    for scope in expected_scopes:
        assert scope in requested_scopes
    for scope in forbidden_scopes:
        assert scope not in requested_scopes
    assert params["include_granted_scopes"] == ["true"]
    assert params["access_type"] == ["offline"]


def test_google_authorization_rejects_unknown_service():
    with pytest.raises(ValueError, match="Unsupported Google service"):
        GoogleOAuthClient.scopes_for_service("drive")


@pytest.mark.parametrize(
    ("existing_scopes", "returned_scopes", "expected_scopes"),
    [
        (
            [
                "openid",
                "email",
                "https://www.googleapis.com/auth/gmail.readonly",
            ],
            [
                "openid",
                "email",
                GoogleOAuthClient.CALENDAR_EVENTS_READ_SCOPE,
                GoogleOAuthClient.CALENDAR_FREEBUSY_SCOPE,
            ],
            [
                "openid",
                "email",
                "https://www.googleapis.com/auth/gmail.readonly",
                GoogleOAuthClient.CALENDAR_EVENTS_READ_SCOPE,
                GoogleOAuthClient.CALENDAR_FREEBUSY_SCOPE,
            ],
        ),
        (
            [
                "openid",
                "email",
                "https://www.googleapis.com/auth/gmail.readonly",
            ],
            [
                "openid",
                "email",
                GoogleOAuthClient.CALENDAR_EVENTS_READ_SCOPE,
            ],
            [
                "openid",
                "email",
                "https://www.googleapis.com/auth/gmail.readonly",
                GoogleOAuthClient.CALENDAR_EVENTS_READ_SCOPE,
            ],
        ),
        (
            [
                "openid",
                "email",
                "https://www.googleapis.com/auth/gmail.readonly",
                "https://www.googleapis.com/auth/calendar.events",
            ],
            [
                "openid",
                "email",
                GoogleOAuthClient.CALENDAR_EVENTS_READ_SCOPE,
                GoogleOAuthClient.CALENDAR_FREEBUSY_SCOPE,
            ],
            [
                "openid",
                "email",
                "https://www.googleapis.com/auth/gmail.readonly",
                "https://www.googleapis.com/auth/calendar.events",
                GoogleOAuthClient.CALENDAR_EVENTS_READ_SCOPE,
                GoogleOAuthClient.CALENDAR_FREEBUSY_SCOPE,
            ],
        ),
        (
            [
                "openid",
                "https://www.googleapis.com/auth/calendar.events",
            ],
            ["openid", "email", "https://www.googleapis.com/auth/gmail.send"],
            [
                "openid",
                "https://www.googleapis.com/auth/calendar.events",
                "email",
                "https://www.googleapis.com/auth/gmail.send",
            ],
        ),
        (
            [
                "openid",
                "https://www.googleapis.com/auth/gmail.readonly",
            ],
            ["openid", "email"],
            [
                "openid",
                "https://www.googleapis.com/auth/gmail.readonly",
                "email",
            ],
        ),
    ],
)
def test_callback_merges_actual_scope_grants_and_preserves_refresh_token(
    existing_scopes,
    returned_scopes,
    expected_scopes,
):
    user_id = uuid4()
    connection_id = uuid4()
    db = Mock()
    service = GoogleOAuthCallbackService(db)
    service.state_service = Mock()
    service.state_service.consume_state.return_value = {
        "user_id": user_id,
        "code_verifier": "verifier",
        "redirect_uri": "https://example.test/callback",
    }
    service.oauth_client = Mock()
    service.oauth_client.exchange_authorization_code = AsyncMock(
        return_value={
            "access_token": "new-access",
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": " ".join(returned_scopes),
        }
    )
    service.oauth_client.get_user_info = AsyncMock(
        return_value={
            "provider_account_id": "google-user-id",
            "email": "user@example.com",
        }
    )
    service.connection_service = Mock()
    service.connection_service.connect_account.return_value = {
        "id": connection_id,
        "action": "reconnected",
    }
    service.credential_service = Mock()
    service.credential_service.get_credentials_for_update.return_value = {
        "refresh_token": "existing-refresh",
        "scopes": existing_scopes,
    }

    result = asyncio.run(
        service.handle_callback(
            code="authorization-code",
            state="opaque-state",
        )
    )

    service.credential_service.save_credentials.assert_called_once()
    saved_credentials = (
        service.credential_service.save_credentials.call_args.kwargs
    )
    assert saved_credentials["scopes"] == expected_scopes
    assert saved_credentials["refresh_token"] is None
    assert result["status"] == "connected"
    db.commit.assert_called_once_with()


def test_declined_calendar_authorization_does_not_change_existing_grants():
    service = GoogleOAuthCallbackService(db=Mock())
    service.state_service = Mock()
    service.state_service.consume_state.return_value = {
        "user_id": uuid4(),
        "code_verifier": "verifier",
        "redirect_uri": "https://example.test/callback",
    }
    service.credential_service = Mock()

    with pytest.raises(ValueError, match="authorization was not completed"):
        asyncio.run(
            service.handle_callback(
                code=None,
                state="opaque-state",
                error="access_denied",
            )
        )

    service.credential_service.save_credentials.assert_not_called()


def test_disconnect_cancels_an_in_flight_oauth_callback():
    service = GoogleOAuthCallbackService(db=Mock())
    service.state_service = Mock()
    service.state_service.consume_state.return_value = {
        "user_id": uuid4(),
        "code_verifier": "verifier",
        "redirect_uri": "https://example.test/callback",
    }
    service.state_service.lock_consumed_state_for_callback.return_value = False
    service.oauth_client = Mock()
    service.oauth_client.exchange_authorization_code = AsyncMock(
        return_value={
            "access_token": "access-token",
            "expires_in": 3600,
            "scope": "https://www.googleapis.com/auth/gmail.readonly",
        }
    )
    service.oauth_client.get_user_info = AsyncMock(
        return_value={
            "provider_account_id": "google-user-id",
            "email": "user@example.com",
        }
    )
    service.connection_service = Mock()
    service.credential_service = Mock()

    with pytest.raises(ValueError, match="canceled by disconnect"):
        asyncio.run(
            service.handle_callback(
                code="authorization-code",
                state="opaque-state",
            )
        )

    service.connection_service.connect_account.assert_not_called()
    service.credential_service.save_credentials.assert_not_called()


def test_credential_update_preserves_existing_refresh_token(monkeypatch):
    credential = SimpleNamespace(
        refresh_token_encrypted="encrypted-old-refresh",
        scopes=[
            "openid",
            UserConnectionService.CALENDAR_EVENTS_READ_SCOPE,
        ],
    )
    repository = Mock()
    repository.get_by_connection.return_value = credential
    repository.update_tokens.return_value = SimpleNamespace(
        connection_id=uuid4(),
        user_id=uuid4(),
        token_type="Bearer",
        expires_at=datetime.now(timezone.utc),
        scopes=credential.scopes,
    )
    service = OAuthCredentialService(db=None)
    service.repository = repository
    monkeypatch.setattr(
        "app.services.oauth_credential_service.encrypt_data",
        lambda value: f"encrypted:{value}",
    )

    service.update_credentials(
        user_id=uuid4(),
        connection_id=uuid4(),
        access_token="new-access",
    )

    assert repository.update_tokens.call_args.kwargs[
        "refresh_token_encrypted"
    ] == "encrypted-old-refresh"
    assert repository.update_tokens.call_args.kwargs["scopes"] == credential.scopes
