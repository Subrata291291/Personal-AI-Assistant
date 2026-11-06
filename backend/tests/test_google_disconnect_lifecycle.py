import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call
from uuid import uuid4

import pytest

from app.integrations.google.api_client import GoogleAPIClient
from app.integrations.google.exceptions import (
    GoogleAPIError,
    GoogleOAuthAuthorizationError,
    GoogleOAuthTransientError,
)
from app.services.action_approval_service import (
    ActionApprovalService,
    ApprovalConflictError,
)
from app.services.calendar_service import CalendarService
from app.services.gmail_service import GmailService
from app.services.oauth_credential_service import OAuthCredentialService


USER_ID = uuid4()
CONNECTION_ID = uuid4()


@pytest.mark.parametrize("operation", ["read", "send"])
def test_disconnected_google_connection_cannot_read_or_send_gmail(operation):
    service = GmailService(db=None)
    service.connection_repository = Mock()
    service.connection_repository.get_active_google_connection.return_value = None
    service.credential_repository = Mock()
    service._google_get = AsyncMock()
    service._google_post = AsyncMock()

    if operation == "read":
        call = service.list_messages(
            user_id=USER_ID,
            connection_id=CONNECTION_ID,
        )
    else:
        call = service.send_message(
            user_id=USER_ID,
            connection_id=CONNECTION_ID,
            to=["recipient@example.com"],
            subject="Test",
            body="Test",
        )

    with pytest.raises(ValueError, match="Connected Google account"):
        asyncio.run(call)

    service._google_get.assert_not_awaited()
    service._google_post.assert_not_awaited()
    service.credential_repository.get_by_connection.assert_not_called()


def test_disconnected_connection_cannot_refresh_a_cached_access_token(
    monkeypatch,
):
    credential_service = OAuthCredentialService(db=None)
    credential_service.repository = Mock()
    credential_service.repository.get_by_connection_for_update.return_value = None
    refresh = AsyncMock()
    monkeypatch.setattr(
        "app.services.oauth_credential_service.GoogleOAuthClient",
        lambda: Mock(refresh_access_token=refresh),
    )

    with pytest.raises(ValueError, match="OAuth credentials not found"):
        asyncio.run(
            credential_service.get_valid_access_token(
                user_id=USER_ID,
                connection_id=CONNECTION_ID,
            )
        )

    refresh.assert_not_awaited()


def test_disconnected_connection_rejects_google_request_before_transmission(
    monkeypatch,
):
    token_service = Mock()
    token_service.get_valid_access_token = AsyncMock(return_value="cached-token")
    monkeypatch.setattr(
        "app.integrations.google.api_client.OAuthCredentialService",
        lambda _db: token_service,
    )
    db = Mock()
    db.scalar.return_value = None
    http_client = Mock()
    http_client.request = AsyncMock()

    with pytest.raises(ValueError, match="Connected Google account"):
        asyncio.run(
            GoogleAPIClient().request(
                db=db,
                user_id=str(USER_ID),
                connection_id=str(CONNECTION_ID),
                method="GET",
                url="https://gmail.googleapis.com/gmail/v1/users/me/messages",
                http_client=http_client,
            )
        )

    http_client.request.assert_not_awaited()


def test_revoked_google_refresh_marks_connection_disconnected(
    monkeypatch,
):
    token_service = Mock()
    token_service.get_valid_access_token = AsyncMock(
        side_effect=GoogleOAuthAuthorizationError(),
    )
    connection_repository = Mock()
    connection_repository.mark_disconnected.return_value = True
    credential_repository = Mock()
    credential_repository.delete.return_value = True
    monkeypatch.setattr(
        "app.integrations.google.api_client.OAuthCredentialService",
        lambda _db: token_service,
    )
    monkeypatch.setattr(
        "app.integrations.google.api_client.UserConnectionRepository",
        lambda _db: connection_repository,
    )
    monkeypatch.setattr(
        "app.integrations.google.api_client.OAuthCredentialRepository",
        lambda _db: credential_repository,
    )
    db = Mock()
    http_client = Mock()
    http_client.request = AsyncMock()

    with pytest.raises(GoogleOAuthAuthorizationError):
        asyncio.run(
            GoogleAPIClient().request(
                db=db,
                user_id=str(USER_ID),
                connection_id=str(CONNECTION_ID),
                method="GET",
                url=(
                    "https://calendar.googleapis.com/calendar/v3/users/me/"
                    "calendarList"
                ),
                http_client=http_client,
            )
        )

    connection_repository.mark_disconnected.assert_called_once_with(
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
    )
    credential_repository.delete.assert_called_once_with(
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
    )
    db.commit.assert_called_once()
    http_client.request.assert_not_awaited()


def test_revoked_google_refresh_during_mutation_cleans_only_selected_connection(
    monkeypatch,
):
    token_service = Mock()
    token_service.get_valid_access_token = AsyncMock(
        side_effect=GoogleOAuthAuthorizationError(),
    )
    connection_repository = Mock()
    connection_repository.mark_disconnected.return_value = True
    credential_repository = Mock()
    credential_repository.delete.return_value = True
    monkeypatch.setattr(
        "app.integrations.google.api_client.OAuthCredentialService",
        lambda _db: token_service,
    )
    monkeypatch.setattr(
        "app.integrations.google.api_client.UserConnectionRepository",
        lambda _db: connection_repository,
    )
    monkeypatch.setattr(
        "app.integrations.google.api_client.OAuthCredentialRepository",
        lambda _db: credential_repository,
    )
    db = Mock()
    http_client = Mock()
    http_client.request = AsyncMock()

    with pytest.raises(GoogleOAuthAuthorizationError):
        asyncio.run(
            GoogleAPIClient().request_mutation(
                db=db,
                user_id=str(USER_ID),
                connection_id=str(CONNECTION_ID),
                method="POST",
                url="https://calendar.googleapis.com/calendar/v3/calendars/primary/events",
                json_data={},
                http_client=http_client,
            )
        )

    token_service.get_valid_access_token.assert_awaited_once_with(
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
    )
    connection_repository.mark_disconnected.assert_called_once_with(
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
    )
    credential_repository.delete.assert_called_once_with(
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
    )
    db.commit.assert_called_once()
    http_client.request.assert_not_awaited()


def test_revoked_google_token_after_api_401_cleans_only_selected_connection(
    monkeypatch,
):
    authorization_error = GoogleOAuthAuthorizationError()
    token_service = Mock()
    token_service.get_valid_access_token = AsyncMock(
        side_effect=["old-access-token", authorization_error],
    )
    connection_repository = Mock()
    connection_repository.get_active_google_connection_for_update.return_value = (
        SimpleNamespace(id=CONNECTION_ID)
    )
    connection_repository.mark_disconnected.return_value = True
    credential_repository = Mock()
    credential_repository.get_by_connection.return_value = SimpleNamespace(
        connection_id=CONNECTION_ID,
    )
    credential_repository.delete.return_value = True
    monkeypatch.setattr(
        "app.integrations.google.api_client.OAuthCredentialService",
        lambda _db: token_service,
    )
    monkeypatch.setattr(
        "app.integrations.google.api_client.UserConnectionRepository",
        lambda _db: connection_repository,
    )
    monkeypatch.setattr(
        "app.integrations.google.api_client.OAuthCredentialRepository",
        lambda _db: credential_repository,
    )
    db = Mock()
    client = GoogleAPIClient()
    client._send_request = AsyncMock(
        side_effect=GoogleAPIError(401, "Google API request was rejected."),
    )

    with pytest.raises(GoogleOAuthAuthorizationError) as raised:
        asyncio.run(
            client.request(
                db=db,
                user_id=str(USER_ID),
                connection_id=str(CONNECTION_ID),
                method="GET",
                url="https://www.googleapis.com/calendar/v3/users/me/calendarList",
                http_client=Mock(),
            )
        )

    assert raised.value is authorization_error
    token_service.get_valid_access_token.assert_has_awaits([
        call(user_id=USER_ID, connection_id=CONNECTION_ID),
        call(
            user_id=USER_ID,
            connection_id=CONNECTION_ID,
            rejected_access_token="old-access-token",
        ),
    ])
    connection_repository.mark_disconnected.assert_called_once_with(
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
    )
    credential_repository.delete.assert_called_once_with(
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
    )
    connection_repository.get_active_google_connection_for_update.assert_called_once_with(
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
    )
    credential_repository.get_by_connection.assert_called_once_with(
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
    )
    db.commit.assert_has_calls([call(), call()])
    db.rollback.assert_has_calls([call(), call()])


def test_revoked_google_token_cleanup_failure_is_surfaced(monkeypatch):
    token_service = Mock()
    token_service.get_valid_access_token = AsyncMock(
        side_effect=[
            "old-access-token",
            GoogleOAuthAuthorizationError(),
        ],
    )
    connection_repository = Mock()
    connection_repository.get_active_google_connection_for_update.return_value = (
        SimpleNamespace(id=CONNECTION_ID)
    )
    connection_repository.mark_disconnected.side_effect = OSError(
        "database write failed",
    )
    monkeypatch.setattr(
        "app.integrations.google.api_client.OAuthCredentialService",
        lambda _db: token_service,
    )
    monkeypatch.setattr(
        "app.integrations.google.api_client.UserConnectionRepository",
        lambda _db: connection_repository,
    )
    credential_repository = Mock()
    monkeypatch.setattr(
        "app.integrations.google.api_client.OAuthCredentialRepository",
        lambda _db: credential_repository,
    )
    db = Mock()
    client = GoogleAPIClient()
    client._send_request = AsyncMock(
        side_effect=GoogleAPIError(401, "Google API request was rejected."),
    )

    with pytest.raises(
        RuntimeError,
        match="Google authorization expired, but connection status could not be updated.",
    ) as raised:
        asyncio.run(
            client.request(
                db=db,
                user_id=str(USER_ID),
                connection_id=str(CONNECTION_ID),
                method="GET",
                url="https://www.googleapis.com/calendar/v3/users/me/calendarList",
                http_client=Mock(),
            )
        )

    assert isinstance(raised.value.__cause__, OSError)
    connection_repository.mark_disconnected.assert_called_once_with(
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
    )
    credential_repository.delete.assert_not_called()
    db.rollback.assert_has_calls([call(), call(), call()])
    db.commit.assert_called_once()


def test_transient_mutation_token_failure_does_not_clear_credentials(
    monkeypatch,
):
    token_service = Mock()
    token_service.get_valid_access_token = AsyncMock(
        side_effect=GoogleOAuthTransientError(),
    )
    connection_repository = Mock()
    credential_repository = Mock()
    monkeypatch.setattr(
        "app.integrations.google.api_client.OAuthCredentialService",
        lambda _db: token_service,
    )
    monkeypatch.setattr(
        "app.integrations.google.api_client.UserConnectionRepository",
        lambda _db: connection_repository,
    )
    monkeypatch.setattr(
        "app.integrations.google.api_client.OAuthCredentialRepository",
        lambda _db: credential_repository,
    )
    db = Mock()
    http_client = Mock()
    http_client.request = AsyncMock()

    with pytest.raises(GoogleOAuthTransientError):
        asyncio.run(
            GoogleAPIClient().request_mutation(
                db=db,
                user_id=str(USER_ID),
                connection_id=str(CONNECTION_ID),
                method="POST",
                url="https://calendar.googleapis.com/calendar/v3/calendars/primary/events",
                json_data={},
                http_client=http_client,
            )
        )

    db.rollback.assert_called_once()
    connection_repository.mark_disconnected.assert_not_called()
    credential_repository.delete.assert_not_called()
    http_client.request.assert_not_awaited()


def test_successful_mutation_uses_selected_user_and_connection(monkeypatch):
    token_service = Mock()
    token_service.get_valid_access_token = AsyncMock(return_value="access-token")
    connection_repository = Mock()
    connection_repository.get_active_google_connection_for_update.return_value = (
        SimpleNamespace(id=CONNECTION_ID)
    )
    credential_repository = Mock()
    credential_repository.get_by_connection.return_value = SimpleNamespace(
        connection_id=CONNECTION_ID,
    )
    monkeypatch.setattr(
        "app.integrations.google.api_client.OAuthCredentialService",
        lambda _db: token_service,
    )
    monkeypatch.setattr(
        "app.integrations.google.api_client.UserConnectionRepository",
        lambda _db: connection_repository,
    )
    monkeypatch.setattr(
        "app.integrations.google.api_client.OAuthCredentialRepository",
        lambda _db: credential_repository,
    )
    db = Mock()
    http_client = Mock()
    http_client.request = AsyncMock(
        return_value=SimpleNamespace(
            is_success=True,
            status_code=200,
            content=b"{}",
            json=lambda: {},
        )
    )

    result = asyncio.run(
        GoogleAPIClient().request_mutation(
            db=db,
            user_id=str(USER_ID),
            connection_id=str(CONNECTION_ID),
            method="POST",
            url="https://calendar.googleapis.com/calendar/v3/calendars/primary/events",
            json_data={},
            http_client=http_client,
        )
    )

    assert result == {}
    token_service.get_valid_access_token.assert_awaited_once_with(
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
    )
    connection_repository.get_active_google_connection_for_update.assert_called_once_with(
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
    )
    credential_repository.get_by_connection.assert_called_once_with(
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
    )
    http_client.request.assert_awaited_once()
    db.commit.assert_has_calls([call(), call()])


@pytest.mark.parametrize("operation", ["events", "freebusy"])
def test_disconnected_google_connection_cannot_read_calendar(operation):
    service = CalendarService(db=None)
    service.connection_repository = Mock()
    service.connection_repository.get_active_google_connection.return_value = None
    service.credential_repository = Mock()
    service._google_get = AsyncMock()
    service._freebusy_query = AsyncMock()
    time_min = datetime(2026, 11, 6, tzinfo=timezone.utc)
    time_max = datetime(2026, 11, 7, tzinfo=timezone.utc)

    if operation == "events":
        call = service.list_events(
            user_id=USER_ID,
            connection_id=CONNECTION_ID,
        )
    else:
        call = service.query_freebusy(
            user_id=USER_ID,
            connection_id=CONNECTION_ID,
            calendar_ids=["primary"],
            time_min=time_min,
            time_max=time_max,
            time_zone="UTC",
        )

    with pytest.raises(ValueError, match="Connected Google account"):
        asyncio.run(call)

    service._google_get.assert_not_awaited()
    service._freebusy_query.assert_not_awaited()
    service.credential_repository.get_by_connection.assert_not_called()


@pytest.mark.parametrize(
    "action_type",
    ["gmail.send", "gmail.send_draft", "calendar.create_event"],
)
def test_approval_connection_validation_rejects_disconnected_accounts(
    action_type,
):
    service = object.__new__(ActionApprovalService)
    service.connection_repository = Mock()
    service.connection_repository.get_active_google_connection.return_value = None
    service.credential_repository = Mock()

    with pytest.raises(LookupError, match="Connected Google account"):
        service._validate_connection(
            user_id=USER_ID,
            connection_id=CONNECTION_ID,
            action_type=action_type,
        )

    service.credential_repository.get_by_connection.assert_not_called()


@pytest.mark.parametrize("action_type", ["gmail.send_draft", "calendar.create_event"])
@pytest.mark.parametrize("proposal_status", ["pending", "approved"])
def test_disconnected_approval_cannot_be_claimed(action_type, proposal_status):
    service = object.__new__(ActionApprovalService)
    service.connection_repository = Mock()
    service.connection_repository.get_active_google_connection.return_value = None
    service.credential_repository = Mock()
    service.calendar_operation_repository = Mock()
    service.get_proposal = Mock(return_value=SimpleNamespace(
        action_type=action_type,
        status=proposal_status,
        connection_id=CONNECTION_ID,
    ))
    service.repository = Mock()

    if proposal_status == "pending":
        with pytest.raises(ApprovalConflictError):
            if action_type.startswith("gmail."):
                service.claim_execution(USER_ID, uuid4())
            else:
                service.claim_calendar_execution(USER_ID, uuid4())
    else:
        with pytest.raises(LookupError, match="Connected Google account"):
            if action_type.startswith("gmail."):
                service.claim_execution(USER_ID, uuid4())
            else:
                service.claim_calendar_execution(USER_ID, uuid4())

    if action_type.startswith("gmail.") and proposal_status == "pending":
        service.repository.transition.assert_called_once()
        assert service.repository.transition.call_args.kwargs[
            "new_status"
        ] == "expired"
    else:
        service.repository.transition.assert_not_called()
    service.calendar_operation_repository.claim_execution.assert_not_called()
