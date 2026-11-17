from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.api.v1.endpoints import drive as drive_endpoint
from app.api.v1.endpoints import oauth as oauth_endpoint
from app.core.config import settings
from app.main import app
from app.services.user_connection_service import UserConnectionService
from app.workflows.chat_graph import ASSISTANT_ROUTE_NODES, chat_graph
from app.workflows.chat_nodes import capabilities_node, integration_unavailable_node
from app.workflows.gmail_agent_nodes import build_runtime_capabilities


USER_ID = UUID("00000000-0000-0000-0000-000000000111")


@pytest.fixture
def client():
    app.dependency_overrides[deps.get_current_user] = lambda: {
        "id": str(USER_ID),
        "user_metadata": {},
    }
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_drive_feature_flag_disabled():
    # Default config is disabled; runtime enforcement below is authoritative even
    # if a local .env overrides the flag during development.
    assert isinstance(settings.DRIVE_CONTRACTS_ENABLED, bool)


def test_drive_endpoints_return_consistent_unavailable_response(client):
    expected = (
        "Google Drive integration is temporarily unavailable and will be "
        "available in a future update."
    )
    cases = [
        ("get", "/api/v1/drive/picker-config"),
        ("get", "/api/v1/drive/picker-token"),
        ("get", "/api/v1/drive/files"),
    ]
    for method, path in cases:
        res = getattr(client, method)(path)
        assert res.status_code == 503, (method, path, res.text)
        assert res.json().get("detail") == expected


def test_oauth_rejects_drive_services_without_breaking_other_services(client, monkeypatch):
    # Drive incremental consent/connect flows must be disabled.
    for service in ("drive_file", "drive_read", "drive_write"):
        res = client.post(f"/api/v1/oauth/google/connect?service={service}")
        assert res.status_code == 503
        assert "temporarily unavailable" in (res.json().get("detail") or "").lower()

    # Gmail connect flow remains available (scope/config validation path, not Drive).
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setattr(settings, "GOOGLE_REDIRECT_URI", "https://example.com/api/v1/oauth/google/callback")

    class _StateService:
        def __init__(self, _db):
            pass
        def create_state(self, **_kwargs):
            return {
                "state": "state12345678901234567890123456789012",
                "code_challenge": "a" * 43,
                "expires_at": "2026-12-31T00:00:00+00:00",
            }

    class _OAuthClient:
        def build_authorization_url(self, **_kwargs):
            return "https://accounts.google.com/o/oauth2/v2/auth?x=1"

    monkeypatch.setattr(oauth_endpoint, "OAuthStateService", _StateService)
    monkeypatch.setattr(oauth_endpoint, "GoogleOAuthClient", lambda: _OAuthClient())

    res = client.post("/api/v1/oauth/google/connect?service=gmail")
    assert res.status_code == 200
    assert "authorization_url" in res.json()


def test_drive_scopes_not_requested_for_non_drive_services_and_scope_isolation():
    # OAuth scope selection must exclude Drive scopes for Gmail/Calendar/Contacts.
    from app.integrations.google.oauth_client import GoogleOAuthClient

    for service in ("gmail", "calendar", "calendar_write", "contacts_read"):
        scopes = GoogleOAuthClient.scopes_for_service(service)
        joined = " ".join(scopes)
        assert "drive" not in joined.lower(), (service, scopes)

    # Existing stored Drive grants must not activate Drive capabilities/access.
    caps = UserConnectionService.aggregate_drive_capabilities([])
    assert caps["connected"] is False
    assert caps["contracts_read"] is False
    assert caps.get("temporarily_unavailable") is True

    svc = UserConnectionService(db=None)
    # Monkeypatch list_connections to simulate stored Drive grants; service must still report unavailable.
    svc.list_connections = lambda user_id: [{
        "id": UUID("00000000-0000-0000-0000-000000000222"),
        "provider": "google",
        "email": "u@example.com",
        "status": "connected",
        "connected_at": None,
        "created_at": None,
        "capabilities": {
            "drive": True,
            "drive_read": True,
            "drive_file": True,
            "drive_contracts_read": True,
            "drive_write_permission_granted": False,
        },
    }]
    access = svc.get_user_drive_access(user_id=USER_ID, default_connection_id=None)
    assert access["enabled"] is False
    assert access["connection_id"] is None
    assert access["capabilities"]["connected"] is False
    assert access["capabilities"]["contracts_read"] is False


def test_drive_not_routed_or_invoked_in_workflow_capabilities():
    assert ASSISTANT_ROUTE_NODES["google_drive"] == "integration_unavailable"

    caps = build_runtime_capabilities(
        granted_scopes=set(),
        unavailable_routes={"google_drive", "tasks"},
        drive_capabilities={"connected": True, "contracts_read": True, "integration_enabled": True},
    )
    assert caps["google_drive"]["chat_available"] is False
    assert caps["google_drive"]["connected"] is False
    assert caps["google_drive"]["integration_enabled"] is False

    unavailable = integration_unavailable_node({"user_message": "show my contract", "assistant_route": "google_drive"})
    assert "temporarily unavailable" in unavailable["assistant_response"].lower()

    cap_answer = capabilities_node({"user_message": "what can you do?", "assistant_capabilities": caps})
    assert "google drive integration is temporarily unavailable" in cap_answer["assistant_response"].lower()
    assert "gmail" in cap_answer["assistant_response"].lower() or "Google Calendar" in cap_answer["assistant_response"]
