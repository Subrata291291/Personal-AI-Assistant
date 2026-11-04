from urllib.parse import parse_qs, urlsplit

import pytest

from app.api.v1.endpoints.oauth import _frontend_connector_url
from app.core.config import settings
from app.integrations.google.oauth_client import GoogleOAuthClient


def test_google_callback_redirect_uses_production_frontend(monkeypatch):
    monkeypatch.setattr(settings, "APP_ENV", "production")
    monkeypatch.setattr(settings, "FRONTEND_URL", "http://localhost:5173")

    redirect_url = _frontend_connector_url("connected")
    parsed_url = urlsplit(redirect_url)

    assert f"{parsed_url.scheme}://{parsed_url.netloc}" == (
        "https://www.myorbitai.in"
    )
    assert parsed_url.path == "/dashboard/connectors"
    assert parse_qs(parsed_url.query) == {"google": ["connected"]}


def test_google_callback_redirect_preserves_local_development(monkeypatch):
    monkeypatch.setattr(settings, "APP_ENV", "development")
    monkeypatch.setattr(settings, "FRONTEND_URL", "http://localhost:5173")

    redirect_url = _frontend_connector_url("error")
    parsed_url = urlsplit(redirect_url)

    assert f"{parsed_url.scheme}://{parsed_url.netloc}" == (
        "http://localhost:5173"
    )
    assert parsed_url.path == "/dashboard/connectors"
    assert parse_qs(parsed_url.query) == {"google": ["error"]}


def test_google_callback_redirect_rejects_untrusted_status(monkeypatch):
    monkeypatch.setattr(settings, "APP_ENV", "production")
    monkeypatch.setattr(
        settings,
        "FRONTEND_URL",
        "https://www.myorbitai.in",
    )

    with pytest.raises(ValueError, match="Invalid Google OAuth callback status"):
        _frontend_connector_url("https://attacker.example")


@pytest.mark.parametrize(
    "redirect_uri",
    [
        "https://www.myorbitai.in/login",
        "https://www.myorbitai.in/",
    ],
)
def test_google_oauth_rejects_frontend_redirect_uri(redirect_uri, monkeypatch):
    monkeypatch.setattr(settings, "APP_ENV", "production")

    with pytest.raises(ValueError, match="backend OAuth callback"):
        GoogleOAuthClient._validate_redirect_uri(redirect_uri)


def test_google_oauth_accepts_backend_callback_uri(monkeypatch):
    monkeypatch.setattr(settings, "APP_ENV", "production")

    GoogleOAuthClient._validate_redirect_uri(
        "https://personal-ai-assistant-l14k.onrender.com"
        "/api/v1/oauth/google/callback"
    )
