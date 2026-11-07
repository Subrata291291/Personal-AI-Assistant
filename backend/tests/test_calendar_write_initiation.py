from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api.v1.endpoints import oauth
from app.core.config import Settings, settings


FEATURE_FLAGS = (
    "CALENDAR_CREATE_EVENT_ENABLED",
    "CALENDAR_UPDATE_EVENT_ENABLED",
    "CALENDAR_DELETE_EVENT_ENABLED",
)


@pytest.mark.parametrize("flag_name", FEATURE_FLAGS)
def test_calendar_write_feature_flags_default_to_false(
    flag_name,
    monkeypatch,
):
    monkeypatch.delenv(flag_name, raising=False)

    default_settings = Settings(_env_file=None)

    assert getattr(default_settings, flag_name) is False


class FakeDatabase:
    def __init__(self, connection):
        self.connection = connection
        self.lookup_parameters = None
        self.rollback = Mock()

    def scalar(self, statement):
        self.lookup_parameters = statement.compile().params
        assert self.connection.id in self.lookup_parameters.values()
        assert self.initiating_user_id in self.lookup_parameters.values()
        if (
            self.connection.id == self.selected_connection_id
            and self.connection.user_id == self.initiating_user_id
        ):
            return self.connection
        return None


def _call_calendar_write_initiation(
    monkeypatch,
    *,
    initiating_user_id,
    connection,
    connection_id,
    service="calendar_write",
):
    db = FakeDatabase(connection)
    db.initiating_user_id = initiating_user_id
    db.selected_connection_id = connection_id
    state_service = SimpleNamespace(
        create_state=Mock(return_value={
            "state": "opaque-state",
            "code_challenge": "pkce-challenge",
            "expires_at": datetime(2026, 11, 7, tzinfo=timezone.utc),
        })
    )
    oauth_client = SimpleNamespace(
        build_authorization_url=Mock(
            return_value="https://accounts.example.test/authorize"
        )
    )

    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "test-client")
    monkeypatch.setattr(
        settings,
        "GOOGLE_REDIRECT_URI",
        "https://backend.example.test/api/v1/oauth/google/callback",
    )
    monkeypatch.setattr(
        oauth,
        "OAuthStateService",
        lambda _db: state_service,
    )
    monkeypatch.setattr(
        oauth,
        "GoogleOAuthClient",
        lambda: oauth_client,
    )

    response = oauth.connect_google(
        current_user={"id": str(initiating_user_id)},
        db=db,
        service=service,
        connection_id=connection_id,
    )
    return response, db, state_service, oauth_client, initiating_user_id


def test_calendar_write_initiation_rejects_missing_connection_id(
    monkeypatch,
):
    db = FakeDatabase(connection=None)
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "test-client")
    monkeypatch.setattr(
        settings,
        "GOOGLE_REDIRECT_URI",
        "https://backend.example.test/api/v1/oauth/google/callback",
    )

    with pytest.raises(HTTPException) as raised:
        oauth.connect_google(
            current_user={"id": str(uuid4())},
            db=db,
            service="calendar_write",
            connection_id=None,
        )

    assert raised.value.status_code == 400
    assert "Choose the connected Google account" in raised.value.detail
    assert db.lookup_parameters is None
    db.rollback.assert_called_once()


def test_calendar_write_initiation_rejects_disconnected_connection(
    monkeypatch,
):
    user_id = uuid4()
    connection_id = uuid4()
    connection = SimpleNamespace(
        id=connection_id,
        user_id=user_id,
        provider="google",
        status="disconnected",
    )
    db = FakeDatabase(connection)
    db.initiating_user_id = user_id
    db.selected_connection_id = connection_id
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "test-client")
    monkeypatch.setattr(
        settings,
        "GOOGLE_REDIRECT_URI",
        "https://backend.example.test/api/v1/oauth/google/callback",
    )

    with pytest.raises(HTTPException) as raised:
        oauth.connect_google(
            current_user={"id": str(user_id)},
            db=db,
            service="calendar_write",
            connection_id=connection_id,
        )

    assert raised.value.status_code == 400
    assert "selected Google account is not connected" in raised.value.detail
    db.rollback.assert_called_once()


def test_calendar_write_initiation_rejects_connection_owned_by_another_user(
    monkeypatch,
):
    user_id = uuid4()
    other_user_id = uuid4()
    connection_id = uuid4()
    connection = SimpleNamespace(
        id=connection_id,
        user_id=other_user_id,
        provider="google",
        status="connected",
    )
    db = FakeDatabase(connection)
    db.initiating_user_id = user_id
    db.selected_connection_id = connection_id
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "test-client")
    monkeypatch.setattr(
        settings,
        "GOOGLE_REDIRECT_URI",
        "https://backend.example.test/api/v1/oauth/google/callback",
    )

    with pytest.raises(HTTPException) as raised:
        oauth.connect_google(
            current_user={"id": str(user_id)},
            db=db,
            service="calendar_write",
            connection_id=connection_id,
        )

    assert raised.value.status_code == 400
    assert "selected Google account is not connected" in raised.value.detail
    assert user_id in db.lookup_parameters.values()
    assert connection_id in db.lookup_parameters.values()
    db.rollback.assert_called_once()


def test_active_owned_connection_can_start_incremental_calendar_consent(
    monkeypatch,
):
    user_id = uuid4()
    connection_id = uuid4()
    connection = SimpleNamespace(
        id=connection_id,
        user_id=user_id,
        provider="google",
        status="connected",
    )

    response, db, state_service, oauth_client, actual_user_id = (
        _call_calendar_write_initiation(
            monkeypatch,
            initiating_user_id=user_id,
            connection=connection,
            connection_id=connection_id,
        )
    )

    assert response == {
        "provider": "google",
        "authorization_url": (
            "https://accounts.example.test/authorize"
        ),
        "expires_at": datetime(2026, 11, 7, tzinfo=timezone.utc),
    }
    state_service.create_state.assert_called_once_with(
        user_id=actual_user_id,
        provider="google",
        redirect_uri=(
            "https://backend.example.test/api/v1/oauth/google/callback"
        ),
        service="calendar_write",
        target_connection_id=connection_id,
    )
    oauth_client.build_authorization_url.assert_called_once_with(
        state="opaque-state",
        code_challenge="pkce-challenge",
        service="calendar_write",
    )
    assert actual_user_id in db.lookup_parameters.values()
    assert connection_id in db.lookup_parameters.values()
    db.rollback.assert_not_called()


def test_calendar_list_consent_targets_the_exact_owned_connection(
    monkeypatch,
):
    user_id = uuid4()
    connection_id = uuid4()
    connection = SimpleNamespace(
        id=connection_id,
        user_id=user_id,
        provider="google",
        status="connected",
    )

    response, db, state_service, oauth_client, actual_user_id = (
        _call_calendar_write_initiation(
            monkeypatch,
            initiating_user_id=user_id,
            connection=connection,
            connection_id=connection_id,
            service="calendar_list_read",
        )
    )

    assert response["provider"] == "google"
    state_service.create_state.assert_called_once_with(
        user_id=actual_user_id,
        provider="google",
        redirect_uri=(
            "https://backend.example.test/api/v1/oauth/google/callback"
        ),
        service="calendar_list_read",
        target_connection_id=connection_id,
    )
    oauth_client.build_authorization_url.assert_called_once_with(
        state="opaque-state",
        code_challenge="pkce-challenge",
        service="calendar_list_read",
    )
    assert actual_user_id in db.lookup_parameters.values()
    assert connection_id in db.lookup_parameters.values()
    db.rollback.assert_not_called()
