from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app


def test_cors_allowed_origins_include_local_and_production_frontends():
    origins = settings.allowed_cors_origins

    assert "http://localhost:5173" in origins
    assert "https://www.myorbitai.in" in origins
    assert all(not origin.startswith("*") for origin in origins)


def test_cors_preflight_allows_production_origin():
    client = TestClient(app)

    response = client.options(
        "/api/v1/action-approvals/gmail-drafts/message-123",
        headers={
            "Origin": "https://www.myorbitai.in",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": (
                "authorization,content-type,idempotency-key,x-request-id"
            ),
        },
    )

    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "https://www.myorbitai.in"
    assert response.headers.get("access-control-allow-credentials") == "true"
    assert "POST" in response.headers.get("access-control-allow-methods", "")
    allowed_headers = response.headers.get("access-control-allow-headers", "").lower()
    assert "idempotency-key" in allowed_headers
    assert "x-request-id" in allowed_headers
