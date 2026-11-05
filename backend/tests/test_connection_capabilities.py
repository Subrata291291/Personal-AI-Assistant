from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from app.services.user_connection_service import UserConnectionService


def test_list_connections_reports_service_capabilities_from_granted_scopes():
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
        SimpleNamespace(scopes=["https://www.googleapis.com/auth/calendar.events"]),
    ]

    service = UserConnectionService(db=None)
    service.repository = repository
    service.credential_repository = credentials

    connections = service.list_connections(user_id=user_id)

    repository.list_by_user.assert_called_once_with(user_id=user_id)
    assert connections[0]["capabilities"] == {
        "gmail": True,
        "gmail_draft": False,
        "gmail_send": False,
        "calendar": False,
        "calendar_events_read": False,
        "calendar_freebusy": False,
        "calendar_api_available": False,
        "calendar_api_complete": False,
        "calendar_chat_available": False,
        "calendar_write_available": False,
    }
    assert connections[1]["capabilities"] == {
        "gmail": False,
        "gmail_draft": False,
        "gmail_send": False,
        "calendar": True,
        "calendar_events_read": True,
        "calendar_freebusy": False,
        "calendar_api_available": True,
        "calendar_api_complete": False,
        "calendar_chat_available": True,
        "calendar_write_available": False,
    }
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

    assert connections[0]["capabilities"] == {
        "gmail": False,
        "gmail_draft": False,
        "gmail_send": False,
        "calendar": False,
        "calendar_events_read": False,
        "calendar_freebusy": False,
        "calendar_api_available": False,
        "calendar_api_complete": False,
        "calendar_chat_available": False,
        "calendar_write_available": False,
    }
    credentials.get_by_connection.assert_not_called()


def test_list_connections_reports_both_services_only_when_both_scopes_are_granted():
    user_id = uuid4()
    connection = SimpleNamespace(
        id=uuid4(),
        provider="google",
        email="both@example.com",
        status="connected",
        connected_at=None,
        created_at=None,
    )
    repository = Mock()
    repository.list_by_user.return_value = [connection]
    credentials = Mock()
    credentials.get_by_connection.return_value = SimpleNamespace(
        scopes=[
            UserConnectionService.GMAIL_READ_SCOPE,
            UserConnectionService.GMAIL_DRAFT_SCOPE,
            UserConnectionService.GMAIL_SEND_SCOPE,
            UserConnectionService.CALENDAR_EVENTS_READ_SCOPE,
            UserConnectionService.CALENDAR_FREEBUSY_SCOPE,
        ],
    )

    service = UserConnectionService(db=None)
    service.repository = repository
    service.credential_repository = credentials

    connections = service.list_connections(user_id=user_id)

    assert connections[0]["capabilities"] == {
        "gmail": True,
        "gmail_draft": True,
        "gmail_send": True,
        "calendar": True,
        "calendar_events_read": True,
        "calendar_freebusy": True,
        "calendar_api_available": True,
        "calendar_api_complete": True,
        "calendar_chat_available": True,
        "calendar_write_available": False,
    }
    credentials.get_by_connection.assert_called_once_with(
        user_id=user_id,
        connection_id=connection.id,
    )


def test_legacy_calendar_events_grant_is_partial_and_requests_new_consent():
    user_id = uuid4()
    connection = SimpleNamespace(
        id=uuid4(),
        provider="google",
        email="legacy@example.com",
        status="connected",
        connected_at=None,
        created_at=None,
    )
    repository = Mock()
    repository.list_by_user.return_value = [connection]
    credentials = Mock()
    credentials.get_by_connection.return_value = SimpleNamespace(
        scopes=["https://www.googleapis.com/auth/calendar.events"],
    )

    service = UserConnectionService(db=None)
    service.repository = repository
    service.credential_repository = credentials

    reported = service.list_connections(user_id=user_id)[0]["capabilities"]

    assert reported["calendar"] is True
    assert reported["calendar_events_read"] is True
    assert reported["calendar_freebusy"] is False
    assert reported["calendar_api_available"] is True
    assert reported["calendar_api_complete"] is False
    assert reported["calendar_chat_available"] is True
    assert reported["calendar_write_available"] is False


def test_user_calendar_capabilities_aggregate_only_owned_connections():
    user_id = uuid4()
    service = UserConnectionService(db=None)
    service.list_connections = Mock(return_value=[
        {
            "provider": "google",
            "status": "connected",
            "capabilities": {
                "calendar": True,
                "calendar_events_read": True,
                "calendar_freebusy": False,
                "calendar_api_available": True,
                "calendar_api_complete": False,
            },
        },
        {
            "provider": "google",
            "status": "connected",
            "capabilities": {
                "calendar": True,
                "calendar_events_read": False,
                "calendar_freebusy": True,
                "calendar_api_available": True,
                "calendar_api_complete": False,
            },
        },
        {
            "provider": "other",
            "status": "connected",
            "capabilities": {
                "calendar": True,
                "calendar_events_read": True,
                "calendar_freebusy": True,
                "calendar_api_available": True,
                "calendar_api_complete": True,
            },
        },
    ])

    assert service.get_user_calendar_capabilities(user_id) == {
        "connected": True,
        "events_read": True,
        "freebusy": True,
        "api_available": True,
        "api_complete": True,
        "chat_available": True,
        "google_connection_available": True,
        "permissions_need_consent": False,
        "write_available": False,
    }
    service.list_connections.assert_called_once_with(user_id=user_id)


def test_calendar_access_selects_owned_connections_per_granted_operation():
    user_id = uuid4()
    event_connection_id = uuid4()
    freebusy_connection_id = uuid4()
    connections = [
        SimpleNamespace(
            id=event_connection_id,
            provider="google",
            email="events@example.com",
            status="connected",
            connected_at=None,
            created_at=None,
        ),
        SimpleNamespace(
            id=freebusy_connection_id,
            provider="google",
            email="availability@example.com",
            status="connected",
            connected_at=None,
            created_at=None,
        ),
    ]
    repository = Mock()
    repository.list_by_user.return_value = connections
    credentials = Mock()
    credentials.get_by_connection.side_effect = [
        SimpleNamespace(scopes=[UserConnectionService.CALENDAR_EVENTS_READ_SCOPE]),
        SimpleNamespace(scopes=[UserConnectionService.CALENDAR_FREEBUSY_SCOPE]),
    ]
    service = UserConnectionService(db=None)
    service.repository = repository
    service.credential_repository = credentials

    access = service.get_user_calendar_access(user_id)

    assert access["event_read_connection_id"] == str(event_connection_id)
    assert access["freebusy_connection_id"] == str(freebusy_connection_id)
    assert access["capabilities"]["events_read"] is True
    assert access["capabilities"]["freebusy"] is True
    assert access["capabilities"]["api_complete"] is True
    assert access["event_selection_required"] is False
    assert access["freebusy_selection_required"] is False
    repository.list_by_user.assert_called_once_with(user_id=user_id)


def test_explicit_default_calendar_account_does_not_fall_back_for_missing_scope():
    user_id = uuid4()
    event_connection_id = uuid4()
    freebusy_connection_id = uuid4()
    connections = [
        SimpleNamespace(
            id=event_connection_id,
            provider="google",
            email="events@example.com",
            status="connected",
            connected_at=None,
            created_at=None,
        ),
        SimpleNamespace(
            id=freebusy_connection_id,
            provider="google",
            email="availability@example.com",
            status="connected",
            connected_at=None,
            created_at=None,
        ),
    ]
    repository = Mock()
    repository.list_by_user.return_value = connections
    credentials = Mock()
    credentials.get_by_connection.side_effect = [
        SimpleNamespace(scopes=[UserConnectionService.CALENDAR_EVENTS_READ_SCOPE]),
        SimpleNamespace(scopes=[UserConnectionService.CALENDAR_FREEBUSY_SCOPE]),
    ]
    service = UserConnectionService(db=None)
    service.repository = repository
    service.credential_repository = credentials

    access = service.get_user_calendar_access(
        user_id,
        default_connection_id=str(event_connection_id),
    )

    assert access["event_read_connection_id"] == str(event_connection_id)
    assert access["freebusy_connection_id"] is None
    assert access["default_connection_selected"] is True
    assert access["selected_freebusy_scope_missing"] is True
    assert access["default_connection_invalid"] is False
    repository.list_by_user.assert_called_once_with(user_id=user_id)


def test_multiple_eligible_accounts_require_an_explicit_default():
    user_id = uuid4()
    connections = [
        SimpleNamespace(
            id=uuid4(),
            provider="google",
            email=f"user{index}@example.com",
            status="connected",
            connected_at=None,
            created_at=None,
        )
        for index in range(2)
    ]
    repository = Mock()
    repository.list_by_user.return_value = connections
    credentials = Mock()
    credentials.get_by_connection.return_value = SimpleNamespace(
        scopes=[UserConnectionService.CALENDAR_EVENTS_READ_SCOPE],
    )
    service = UserConnectionService(db=None)
    service.repository = repository
    service.credential_repository = credentials

    access = service.get_user_calendar_access(user_id)

    assert access["event_read_connection_id"] is None
    assert access["event_selection_required"] is True
    assert access["event_candidate_emails"] == [
        "user0@example.com",
        "user1@example.com",
    ]


def test_invalid_or_unowned_default_is_not_selected():
    user_id = uuid4()
    repository = Mock()
    repository.list_by_user.return_value = []
    credentials = Mock()
    service = UserConnectionService(db=None)
    service.repository = repository
    service.credential_repository = credentials

    access = service.get_user_calendar_access(
        user_id,
        default_connection_id=str(uuid4()),
    )

    assert access["default_connection_invalid"] is True
    assert access["event_read_connection_id"] is None
    assert access["freebusy_connection_id"] is None
    repository.list_by_user.assert_called_once_with(user_id=user_id)


def test_aggregate_calendar_capabilities_combine_independent_owned_grants():
    capabilities = UserConnectionService.aggregate_calendar_capabilities([
        {
            "provider": "google",
            "status": "connected",
            "capabilities": {
                "calendar": True,
                "calendar_events_read": True,
                "calendar_freebusy": False,
            },
        },
        {
            "provider": "google",
            "status": "connected",
            "capabilities": {
                "calendar": True,
                "calendar_events_read": False,
                "calendar_freebusy": True,
            },
        },
    ])

    assert capabilities == {
        "connected": True,
        "events_read": True,
        "freebusy": True,
        "api_available": True,
        "api_complete": True,
        "chat_available": True,
        "google_connection_available": True,
        "permissions_need_consent": False,
        "write_available": False,
    }


def test_disconnect_deletes_only_the_authenticated_users_shared_connection():
    user_id = uuid4()
    connection_id = uuid4()
    repository = Mock()
    repository.delete_by_id.return_value = True
    db = Mock()

    service = UserConnectionService(db=db)
    service.repository = repository

    assert service.disconnect_account(
        user_id=user_id,
        connection_id=connection_id,
    )

    repository.delete_by_id.assert_called_once_with(
        user_id=user_id,
        connection_id=connection_id,
    )
    db.commit.assert_called_once_with()


def test_runtime_scopes_are_only_retrieved_for_active_connection_owned_by_user():
    user_id = uuid4()
    connection_id = uuid4()
    repository = Mock()
    repository.get_active_google_connection.return_value = None
    credentials = Mock()

    service = UserConnectionService(db=None)
    service.repository = repository
    service.credential_repository = credentials

    assert service.get_granted_google_scopes(
        user_id=user_id,
        connection_id=connection_id,
    ) is None

    repository.get_active_google_connection.assert_called_once_with(
        user_id=user_id,
        connection_id=connection_id,
    )
    credentials.get_by_connection.assert_not_called()


def test_runtime_scopes_return_only_grants_for_requested_users_connection():
    user_id = uuid4()
    connection_id = uuid4()
    repository = Mock()
    repository.get_active_google_connection.return_value = object()
    credentials = Mock()
    credentials.get_by_connection.return_value = SimpleNamespace(
        scopes=[UserConnectionService.GMAIL_READ_SCOPE],
    )

    service = UserConnectionService(db=None)
    service.repository = repository
    service.credential_repository = credentials

    assert service.get_granted_google_scopes(
        user_id=user_id,
        connection_id=connection_id,
    ) == {UserConnectionService.GMAIL_READ_SCOPE}

    repository.get_active_google_connection.assert_called_once_with(
        user_id=user_id,
        connection_id=connection_id,
    )
    credentials.get_by_connection.assert_called_once_with(
        user_id=user_id,
        connection_id=connection_id,
    )
