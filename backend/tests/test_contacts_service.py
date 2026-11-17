import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from app.services.contacts_service import ContactsService


USER_ID = uuid4()
CONNECTION_ID = uuid4()


def _service_with_mocked_client(request_mock):
    service = ContactsService.__new__(ContactsService)
    service.db = None
    service.connection_repository = SimpleNamespace(
        get_active_google_connection=lambda **_kwargs: SimpleNamespace(
            id=CONNECTION_ID,
        )
    )
    service.credential_repository = SimpleNamespace(
        get_by_connection=lambda **_kwargs: SimpleNamespace(
            scopes=["https://www.googleapis.com/auth/contacts"],
        )
    )
    service.api_client = SimpleNamespace(request=request_mock)
    service.MAX_PAGE_SIZE = 1000
    service._RESOURCE_NAME = ContactsService._RESOURCE_NAME
    service._validate_connection = ContactsService._validate_connection.__get__(
        service, ContactsService
    )
    service._validate_resource_name = ContactsService._validate_resource_name.__get__(
        service, ContactsService
    )
    service._validate_person_fields = ContactsService._validate_person_fields.__get__(
        service, ContactsService
    )
    return service


def test_list_contacts_returns_connections_and_next_page_token():
    request_mock = AsyncMock(return_value={
        "connections": [{"resourceName": "people/1"}],
        "nextPageToken": "token-2",
        "totalItems": 1,
    })
    service = _service_with_mocked_client(request_mock)

    result = asyncio.run(
        service.list_contacts(
            user_id=USER_ID,
            connection_id=CONNECTION_ID,
            page_size=50,
        )
    )

    assert result["connections"] == [{"resourceName": "people/1"}]
    assert result["next_page_token"] == "token-2"
    assert result["total_items"] == 1
    request_mock.assert_awaited_once()


def test_search_contacts_requires_query():
    request_mock = AsyncMock()
    service = _service_with_mocked_client(request_mock)

    try:
        asyncio.run(
            service.search_contacts(
                user_id=USER_ID,
                connection_id=CONNECTION_ID,
                query="   ",
            )
        )
    except ValueError as exc:
        assert "query is required" in str(exc)
    else:
        raise AssertionError("Expected ValueError for empty query.")


def test_search_contacts_forwards_page_token():
    request_mock = AsyncMock(return_value={"results": [], "nextPageToken": None})
    service = _service_with_mocked_client(request_mock)

    asyncio.run(
        service.search_contacts(
            user_id=USER_ID,
            connection_id=CONNECTION_ID,
            query="rintu",
            page_size=10,
            page_token="tok-2",
        )
    )

    _, kwargs = request_mock.await_args
    assert kwargs["params"]["pageToken"] == "tok-2"
    assert kwargs["params"]["query"] == "rintu"


def test_get_contact_validates_resource_name():
    request_mock = AsyncMock(return_value={"resourceName": "people/1"})
    service = _service_with_mocked_client(request_mock)

    result = asyncio.run(
        service.get_contact(
            user_id=USER_ID,
            connection_id=CONNECTION_ID,
            resource_name="people/1",
        )
    )

    assert result["resourceName"] == "people/1"


def test_create_contact_calls_people_api():
    request_mock = AsyncMock(return_value={"resourceName": "people/2"})
    service = _service_with_mocked_client(request_mock)

    result = asyncio.run(
        service.create_contact(
            user_id=USER_ID,
            connection_id=CONNECTION_ID,
            contact={"names": [{"givenName": "Ada"}]},
        )
    )

    assert result["resourceName"] == "people/2"
    request_mock.assert_awaited_once()


def test_update_contact_requires_update_person_fields():
    request_mock = AsyncMock()
    service = _service_with_mocked_client(request_mock)

    try:
        asyncio.run(
            service.update_contact(
                user_id=USER_ID,
                connection_id=CONNECTION_ID,
                resource_name="people/1",
                contact={"names": [{"givenName": "Ada"}]},
                update_person_fields="   ",
            )
        )
    except ValueError as exc:
        assert "updatePersonFields is required" in str(exc)
    else:
        raise AssertionError("Expected ValueError for empty updatePersonFields.")


def test_delete_contact_calls_people_api():
    request_mock = AsyncMock(return_value={})
    service = _service_with_mocked_client(request_mock)

    result = asyncio.run(
        service.delete_contact(
            user_id=USER_ID,
            connection_id=CONNECTION_ID,
            resource_name="people/1",
        )
    )

    assert result == {}
    request_mock.assert_awaited_once()


def test_connection_validation_rejects_missing_scope():
    request_mock = AsyncMock()
    service = ContactsService.__new__(ContactsService)
    service.db = None
    service.connection_repository = SimpleNamespace(
        get_active_google_connection=lambda **_kwargs: SimpleNamespace(
            id=CONNECTION_ID,
        )
    )
    service.credential_repository = SimpleNamespace(
        get_by_connection=lambda **_kwargs: SimpleNamespace(
            scopes=["https://www.googleapis.com/auth/gmail.readonly"],
        )
    )
    service.api_client = SimpleNamespace(request=request_mock)
    service.MAX_PAGE_SIZE = 1000
    service._RESOURCE_NAME = ContactsService._RESOURCE_NAME
    service._validate_connection = ContactsService._validate_connection.__get__(
        service, ContactsService
    )

    try:
        asyncio.run(
            service.list_contacts(
                user_id=USER_ID,
                connection_id=CONNECTION_ID,
            )
        )
    except PermissionError as exc:
        assert "Contacts permission" in str(exc)
    else:
        raise AssertionError("Expected PermissionError for missing scope.")