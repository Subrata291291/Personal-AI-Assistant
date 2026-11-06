from unittest.mock import Mock
from uuid import uuid4

from app.api.v1.endpoints.chat import chat as chat_endpoint
from app.api.v1.endpoints.oauth import (
    list_google_connections,
)
from app.schemas.chat import ChatRequest


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
