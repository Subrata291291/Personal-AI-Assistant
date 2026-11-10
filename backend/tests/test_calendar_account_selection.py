from unittest.mock import Mock
from uuid import uuid4

from fastapi.testclient import TestClient

from app.api import deps
from app.api.v1.endpoints import oauth
from app.core.config import settings
from app.db.session import get_db
from app.main import app
from app.api.v1.endpoints.chat import chat as chat_endpoint
from app.api.v1.endpoints.oauth import (
    list_google_connections,
)
from app.schemas.chat import ChatRequest
from app.services.user_connection_service import UserConnectionService


def test_chat_uses_authenticated_calendar_default_preference():
    user_id = uuid4()
    connection_id = uuid4()
    conversation_id = uuid4()
    idempotency_key = uuid4()
    service = Mock()
    service.send_message.return_value = {"ok": True}

    result = chat_endpoint(
        payload=ChatRequest(
            conversation_id=conversation_id,
            content="Show my upcoming meetings.",
        ),
        user={
            "id": user_id,
            "user_metadata": {
                "default_calendar_connection_id": str(connection_id),
            },
        },
        service=service,
        idempotency_key=idempotency_key,
    )

    assert result == {"ok": True}
    assert service.send_message.call_args.kwargs[
        "default_calendar_connection_id"
    ] == str(connection_id)


def test_connections_endpoint_returns_authoritative_aggregate_capabilities(
    monkeypatch,
):
    user_id = uuid4()
    capabilities = {
        "connected": True,
        "events_read": True,
        "freebusy": True,
        "api_available": True,
        "api_complete": True,
        "chat_available": True,
        "google_connection_available": True,
        "permissions_need_consent": False,
        "write_permission_granted": False,
        "write_execution_available": False,
        "write_available": False,
    }
    expected_user_id = user_id

    class FakeConnectionService:
        def __init__(self, _db):
            pass

        def list_connections(self, *, user_id):
            assert user_id == expected_user_id
            return [{"id": uuid4(), "provider": "google"}]

        @staticmethod
        def aggregate_calendar_capabilities(_connections):
            return capabilities

    monkeypatch.setattr(
        "app.api.v1.endpoints.oauth.UserConnectionService",
        FakeConnectionService,
    )

    response = list_google_connections(
        current_user={"id": user_id},
        db=Mock(),
    )

    assert response["calendar_capabilities"] == capabilities
    assert response["total"] == 1


def test_connections_api_serializes_configured_calendar_capabilities(
    monkeypatch,
):
    user_id = uuid4()
    connection = type("Connection", (), {
        "id": uuid4(),
        "provider": "google",
        "email": "calendar-test@example.invalid",
        "status": "connected",
        "connected_at": None,
        "created_at": None,
    })()
    scopes = [
        UserConnectionService.CALENDAR_EVENTS_READ_SCOPE,
        *UserConnectionService.CALENDAR_LIST_READ_SCOPES,
        *UserConnectionService.CALENDAR_FREEBUSY_SCOPES,
        UserConnectionService.CALENDAR_EVENTS_WRITE_SCOPE,
    ]
    service = UserConnectionService(db=None)
    service.repository = Mock()
    service.repository.list_by_user.return_value = [connection]
    service.credential_repository = Mock()
    service.credential_repository.get_by_connection.return_value = type(
        "Credential",
        (),
        {"scopes": scopes},
    )()
    monkeypatch.setattr(settings, "CALENDAR_CREATE_EVENT_ENABLED", True)
    monkeypatch.setattr(settings, "CALENDAR_UPDATE_EVENT_ENABLED", True)
    monkeypatch.setattr(settings, "CALENDAR_DELETE_EVENT_ENABLED", False)
    monkeypatch.setattr(oauth, "UserConnectionService", lambda _db: service)

    missing = object()
    previous_user_override = app.dependency_overrides.get(
        deps.get_current_user,
        missing,
    )
    previous_db_override = app.dependency_overrides.get(get_db, missing)
    app.dependency_overrides[deps.get_current_user] = lambda: {
        "id": str(user_id),
    }
    app.dependency_overrides[get_db] = lambda: None
    try:
        with TestClient(app) as client:
            response = client.get("/api/v1/oauth/connections")
    finally:
        if previous_user_override is missing:
            app.dependency_overrides.pop(deps.get_current_user, None)
        else:
            app.dependency_overrides[deps.get_current_user] = (
                previous_user_override
            )
        if previous_db_override is missing:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = previous_db_override

    assert response.status_code == 200
    payload = response.json()
    connection_capabilities = payload["connections"][0]["capabilities"]
    assert connection_capabilities["calendar_create_feature_enabled"] is True
    assert connection_capabilities["calendar_update_feature_enabled"] is True
    assert connection_capabilities["calendar_delete_feature_enabled"] is False
    assert connection_capabilities["calendar_create_execution_available"] is True
    assert connection_capabilities["calendar_update_execution_available"] is True
    assert connection_capabilities["calendar_delete_execution_available"] is False
    assert connection_capabilities["calendar_write_execution_available"] is True
    assert connection_capabilities["calendar_write_available"] is True

    aggregate = payload["calendar_capabilities"]
    assert aggregate["write_permission_granted"] is True
    assert aggregate["create_feature_enabled"] is True
    assert aggregate["update_feature_enabled"] is True
    assert aggregate["delete_feature_enabled"] is False
    assert aggregate["create_execution_available"] is True
    assert aggregate["update_execution_available"] is True
    assert aggregate["delete_execution_available"] is False
    assert aggregate["write_execution_available"] is True
    assert aggregate["write_available"] is True
