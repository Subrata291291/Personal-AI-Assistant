import asyncio
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.core import supabase_auth
from app.core.config import settings
from app.main import app


USER_ID = uuid4()
ACCESS_TOKEN = "test-access-token"
VERIFIED_AT = "2026-01-01T00:00:00+00:00"


class AuthResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self.body = body

    def json(self):
        return self.body


class AuthClient:
    def __init__(self, response):
        self.response = response
        self.request = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, url, headers):
        self.request = (url, headers)
        return self.response


def install_supabase_response(monkeypatch, response):
    client = AuthClient(response)
    monkeypatch.setattr(
        supabase_auth.httpx,
        "AsyncClient",
        lambda **_kwargs: client,
    )
    monkeypatch.setattr(settings, "SUPABASE_URL", "https://auth.example.test")
    monkeypatch.setattr(
        settings,
        "SUPABASE_ANON_KEY",
        SecretStr("test-anon-key"),
    )
    return client


def supabase_user(**overrides):
    user = {
        "id": str(USER_ID),
        "email": "user@example.test",
        "email_confirmed_at": VERIFIED_AT,
        "role": "authenticated",
        "app_metadata": {"provider": "email"},
        "user_metadata": {},
    }
    user.update(overrides)
    return user


def get_profile(monkeypatch, auth_response):
    install_supabase_response(monkeypatch, auth_response)
    return TestClient(app).get(
        "/api/v1/me",
        headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
    )


def test_verified_email_password_user_can_access_protected_route(monkeypatch):
    response = get_profile(
        monkeypatch,
        AuthResponse(200, supabase_user()),
    )

    assert response.status_code == 200
    assert response.json() == {
        "user_id": str(USER_ID),
        "email": "user@example.test",
        "role": "authenticated",
    }


def test_unverified_email_user_is_rejected_without_personal_details(
    monkeypatch,
):
    response = get_profile(
        monkeypatch,
        AuthResponse(
            200,
            supabase_user(email_confirmed_at=None),
        ),
    )

    assert response.status_code == 403
    assert response.json() == {
        "detail": "Account email verification is required.",
    }
    assert "user@example.test" not in response.text
    assert ACCESS_TOKEN not in response.text


def test_verified_google_oauth_user_uses_authoritative_supabase_confirmation(
    monkeypatch,
):
    google_user = supabase_user(
        app_metadata={"provider": "google", "providers": ["google"]},
        identities=[
            {
                "provider": "google",
                "identity_data": {
                    "email": "user@example.test",
                    "email_verified": True,
                },
            },
        ],
    )
    response = get_profile(monkeypatch, AuthResponse(200, google_user))

    assert response.status_code == 200
    assert response.json()["user_id"] == str(USER_ID)


@pytest.mark.parametrize(
    "overrides",
    [
        {"email_confirmed_at": None},
        {"email_confirmed_at": ""},
        {"email_confirmed_at": "not-a-timestamp"},
        {"email_confirmed_at": "2026-01-01T00:00:00"},
        {"email": None, "phone": None},
        {
            "email_confirmed_at": None,
            "identities": [
                {
                    "provider": "google",
                    "identity_data": {"email_verified": True},
                },
            ],
        },
    ],
)
def test_missing_or_malformed_verification_data_fails_closed(
    monkeypatch,
    overrides,
):
    response = get_profile(
        monkeypatch,
        AuthResponse(200, supabase_user(**overrides)),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == (
        "Account email verification is required."
    )


@pytest.mark.parametrize("status_code", [401, 403])
def test_invalid_or_expired_supabase_token_remains_unauthorized(
    monkeypatch,
    status_code,
):
    response = get_profile(
        monkeypatch,
        AuthResponse(status_code, {"message": "invalid token"}),
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid or expired access token."
    assert response.headers["www-authenticate"] == "Bearer"


def test_authenticated_identity_remains_supabase_user_id(monkeypatch):
    other_user_id = uuid4()
    response = get_profile(
        monkeypatch,
        AuthResponse(
            200,
            supabase_user(
                id=str(other_user_id),
                user_metadata={"id": str(USER_ID)},
            ),
        ),
    )

    assert response.status_code == 200
    assert response.json()["user_id"] == str(other_user_id)
    assert response.json()["user_id"] != str(USER_ID)


def test_valid_supabase_user_identifier_is_returned_as_uuid(monkeypatch):
    install_supabase_response(
        monkeypatch,
        AuthResponse(200, supabase_user()),
    )

    user = asyncio.run(
        supabase_auth.verify_supabase_token(ACCESS_TOKEN)
    )

    assert user["id"] == UUID(str(USER_ID))
