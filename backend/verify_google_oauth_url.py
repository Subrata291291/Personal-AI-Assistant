
from urllib.parse import urlparse, parse_qs

from app.integrations.google.oauth_client import GoogleOAuthClient
from app.core.config import settings


def verify_google_oauth_url():
    client = GoogleOAuthClient()

    dummy_state = "A" * 43
    dummy_challenge = "B" * 43

    # Temporarily override only in-memory settings.
    original_client_id = settings.GOOGLE_CLIENT_ID
    original_redirect_uri = settings.GOOGLE_REDIRECT_URI

    try:
        settings.GOOGLE_CLIENT_ID = "dummy-client-id"
        settings.GOOGLE_REDIRECT_URI = (
            "http://localhost:8000/api/v1/oauth/google/callback"
        )

        url = client.build_authorization_url(
            state=dummy_state,
            code_challenge=dummy_challenge,
        )

        parsed = urlparse(url)
        params = parse_qs(parsed.query)

        assert parsed.hostname == "accounts.google.com"
        assert params["response_type"] == ["code"]
        assert params["state"] == [dummy_state]
        assert params["code_challenge"] == [dummy_challenge]
        assert params["code_challenge_method"] == ["S256"]
        assert params["access_type"] == ["offline"]

        scopes = params["scope"][0].split()

        assert (
            "https://www.googleapis.com/auth/gmail.readonly"
            in scopes
        )

        assert "openid" in scopes
        assert "email" in scopes
        assert "profile" in scopes

        print("Google authorization endpoint: PASS")
        print("OAuth state parameter: PASS")
        print("PKCE S256 parameter: PASS")
        print("Offline access parameter: PASS")
        print("Gmail read-only scope: PASS")

    finally:
        settings.GOOGLE_CLIENT_ID = original_client_id
        settings.GOOGLE_REDIRECT_URI = original_redirect_uri


if __name__ == "__main__":
    verify_google_oauth_url()
