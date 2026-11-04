from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from app.services.user_connection_service import UserConnectionService


def test_list_connections_reports_gmail_only_when_read_scope_is_granted():
    user_id = uuid4()
    gmail_connection = SimpleNamespace(
        id=uuid4(),
        provider="google",
        email="gmail@example.com",
        status="connected",
        connected_at=None,
        created_at=None,
    )
    other_connection = SimpleNamespace(
        id=uuid4(),
        provider="google",
        email="other@example.com",
        status="connected",
        connected_at=None,
        created_at=None,
    )
    repository = Mock()
    repository.list_by_user.return_value = [
        gmail_connection,
        other_connection,
    ]
    credentials = Mock()
    credentials.get_by_connection.side_effect = [
        SimpleNamespace(scopes=[UserConnectionService.GMAIL_READ_SCOPE]),
        SimpleNamespace(scopes=["openid", "email"]),
    ]

    service = UserConnectionService(db=None)
    service.repository = repository
    service.credential_repository = credentials

    connections = service.list_connections(user_id=user_id)

    repository.list_by_user.assert_called_once_with(user_id=user_id)
    assert connections[0]["capabilities"] == {"gmail": True}
    assert connections[1]["capabilities"] == {"gmail": False}
    assert credentials.get_by_connection.call_args_list[0].kwargs == {
        "user_id": user_id,
        "connection_id": gmail_connection.id,
    }


def test_list_connections_does_not_report_non_google_as_gmail():
    user_id = uuid4()
    connection = SimpleNamespace(
        id=uuid4(),
        provider="other",
        email=None,
        status="connected",
        connected_at=None,
        created_at=None,
    )
    repository = Mock()
    repository.list_by_user.return_value = [connection]
    credentials = Mock()

    service = UserConnectionService(db=None)
    service.repository = repository
    service.credential_repository = credentials

    connections = service.list_connections(user_id=user_id)

    assert connections[0]["capabilities"] == {"gmail": False}
    credentials.get_by_connection.assert_not_called()
