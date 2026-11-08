import asyncio
import httpx
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

from app.core.request_metrics import (
    RequestMetrics,
    current_request_metrics,
    record_timing,
)
from app.integrations.google.api_client import GoogleAPIClient


def test_request_metrics_remain_isolated_under_asyncio_concurrency():
    async def run_request(request_id, stage):
        metrics = RequestMetrics(request_id=request_id)
        token = current_request_metrics.set(metrics)
        try:
            await asyncio.sleep(0)
            record_timing(stage, 25)
            await asyncio.sleep(0)
            return metrics
        finally:
            current_request_metrics.reset(token)

    async def run_all():
        return await asyncio.gather(
            run_request("calendar", "google_calendar_api"),
            run_request("gmail", "gmail_api"),
            run_request("oauth", "google_oauth_api"),
        )

    calendar_metrics, gmail_metrics, oauth_metrics = asyncio.run(run_all())

    assert calendar_metrics.google_calendar_api_call_count == 1
    assert gmail_metrics.gmail_api_call_count == 1
    assert oauth_metrics.google_oauth_call_count == 1
    assert calendar_metrics.gmail_api_call_count == 0
    assert gmail_metrics.google_calendar_api_call_count == 0
    assert oauth_metrics.google_calendar_api_call_count == 0


def test_calendar_provider_attempt_is_attributed_to_calendar_metric():
    async def run():
        metrics = RequestMetrics(request_id="calendar-request")
        token = current_request_metrics.set(metrics)
        try:
            client = GoogleAPIClient()
            http_client = httpx.AsyncClient(transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200,
                    json={"items": []},
                    request=httpx.Request(
                        "GET",
                        "https://calendar.googleapis.com/calendar/v3/"
                        "users/me/calendarList",
                    ),
                )
            ))
            try:
                await client._send_request(
                    client=http_client,
                    method="GET",
                    url=(
                        "https://calendar.googleapis.com/calendar/v3/"
                        "users/me/calendarList"
                    ),
                    headers={"Authorization": "Bearer test"},
                    params=None,
                    json_data=None,
                )
            finally:
                await http_client.aclose()
            return metrics
        finally:
            current_request_metrics.reset(token)

    metrics = asyncio.run(run())
    assert metrics.google_calendar_api_call_count == 1
    assert metrics.gmail_api_call_count == 0


def test_concurrent_calendar_reads_use_normal_connection_select(monkeypatch):
    from app.integrations.google import api_client as api_client_module

    user_id = uuid4()
    connection_id = uuid4()
    token_service = Mock()
    token_service.get_valid_access_token = AsyncMock(return_value="access-token")
    monkeypatch.setattr(
        api_client_module,
        "OAuthCredentialService",
        lambda _db: token_service,
    )

    normal_connection_repository = Mock()
    normal_connection_repository.get_active_google_connection.return_value = (
        SimpleNamespace(id=connection_id, status="connected")
    )
    monkeypatch.setattr(
        api_client_module,
        "UserConnectionRepository",
        lambda _db: normal_connection_repository,
    )

    credential_repository = Mock()
    credential_repository.get_by_connection.return_value = SimpleNamespace(
        connection_id=connection_id,
    )
    monkeypatch.setattr(
        api_client_module,
        "OAuthCredentialRepository",
        lambda _db: credential_repository,
    )

    client = GoogleAPIClient()
    both_started = asyncio.Event()
    request_count = 0

    async def fake_send_request(**_kwargs):
        nonlocal request_count
        request_count += 1
        if request_count == 2:
            both_started.set()
        await both_started.wait()
        return {"items": []}

    client._send_request = fake_send_request

    async def run_reads():
        calendar_url = (
            "https://calendar.googleapis.com/calendar/v3/"
            "users/me/calendarList"
        )
        event_url = (
            "https://calendar.googleapis.com/calendar/v3/"
            "calendars/primary/events"
        )
        return await asyncio.gather(
            client.request(
                db=Mock(),
                user_id=str(user_id),
                connection_id=str(connection_id),
                method="GET",
                url=calendar_url,
                http_client=Mock(),
                allow_concurrent_read=True,
            ),
            client.request(
                db=Mock(),
                user_id=str(user_id),
                connection_id=str(connection_id),
                method="GET",
                url=event_url,
                http_client=Mock(),
                allow_concurrent_read=True,
            ),
        )

    asyncio.run(run_reads())
    assert request_count == 2
    normal_connection_repository.get_active_google_connection.assert_called()
    normal_connection_repository.get_active_google_connection_for_update.assert_not_called()


def test_www_calendar_list_and_event_reads_execute_concurrently(monkeypatch):
    from app.integrations.google import api_client as api_client_module

    user_id = uuid4()
    connection_id = uuid4()
    token_service = Mock()
    token_service.get_valid_access_token = AsyncMock(return_value="access-token")
    monkeypatch.setattr(
        api_client_module,
        "OAuthCredentialService",
        lambda _db: token_service,
    )

    connection_repository = Mock()
    connection_repository.get_active_google_connection.return_value = (
        SimpleNamespace(id=connection_id, status="connected")
    )
    monkeypatch.setattr(
        api_client_module,
        "UserConnectionRepository",
        lambda _db: connection_repository,
    )
    monkeypatch.setattr(
        api_client_module,
        "OAuthCredentialRepository",
        lambda _db: Mock(
            get_by_connection=Mock(return_value=SimpleNamespace(
                connection_id=connection_id,
            )),
        ),
    )

    client = GoogleAPIClient()
    both_started = asyncio.Event()
    request_count = 0

    async def fake_send_request(**_kwargs):
        nonlocal request_count
        request_count += 1
        if request_count == 2:
            both_started.set()
        await both_started.wait()
        return {"items": []}

    client._send_request = fake_send_request

    async def run_reads():
        return await asyncio.gather(
            client.request(
                db=Mock(),
                user_id=str(user_id),
                connection_id=str(connection_id),
                method="GET",
                url=(
                    "https://www.googleapis.com/calendar/v3/"
                    "users/me/calendarList"
                ),
                http_client=Mock(),
                allow_concurrent_read=True,
            ),
            client.request(
                db=Mock(),
                user_id=str(user_id),
                connection_id=str(connection_id),
                method="GET",
                url=(
                    "https://www.googleapis.com/calendar/v3/"
                    "calendars/primary/events"
                ),
                http_client=Mock(),
                allow_concurrent_read=True,
            ),
        )

    asyncio.run(run_reads())
    assert request_count == 2
    connection_repository.get_active_google_connection.assert_called()
    connection_repository.get_active_google_connection_for_update.assert_not_called()


def test_calendar_list_freebusy_and_event_search_are_concurrent_reads():
    client = GoogleAPIClient()

    assert client._is_concurrent_read(
        "GET",
        "https://www.googleapis.com/calendar/v3/users/me/calendarList",
    )
    assert client._is_concurrent_read(
        "GET",
        "https://www.googleapis.com/calendar/v3/"
        "calendars/primary/events?q=gym",
    )
    assert client._is_concurrent_read(
        "POST",
        "https://www.googleapis.com/calendar/v3/freeBusy",
    )


def test_unrelated_post_and_mutation_requests_are_not_concurrent_reads():
    client = GoogleAPIClient()

    assert not client._is_concurrent_read(
        "POST",
        "https://www.googleapis.com/calendar/v3/"
        "calendars/primary/events",
    )
    assert not client._is_concurrent_read(
        "GET",
        "https://www.googleapis.com/drive/v3/files",
    )
    assert not client._is_concurrent_read(
        "DELETE",
        "https://www.googleapis.com/calendar/v3/"
        "calendars/primary/events/event-id",
    )


def test_www_calendar_list_request_is_attributed_to_calendar_metric():
    async def run():
        metrics = RequestMetrics(request_id="calendar-list-request")
        token = current_request_metrics.set(metrics)
        try:
            client = GoogleAPIClient()
            http_client = httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={"items": []},
                    request=request,
                )
            ))
            try:
                await client._send_request(
                    client=http_client,
                    method="GET",
                    url=(
                        "https://www.googleapis.com/calendar/v3/"
                        "users/me/calendarList"
                    ),
                    headers={"Authorization": "******"},
                    params=None,
                    json_data=None,
                )
            finally:
                await http_client.aclose()
            return metrics
        finally:
            current_request_metrics.reset(token)

    metrics = asyncio.run(run())
    assert metrics.google_calendar_api_call_count == 1
    assert metrics.gmail_api_call_count == 0


def test_retry_backoff_is_measured_and_request_id_is_preserved(monkeypatch):
    from app.integrations.google import api_client as api_client_module

    async def run():
        metrics = RequestMetrics(request_id="retry-request-id")
        token = current_request_metrics.set(metrics)
        try:
            attempts = 0

            def handler(request):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    return httpx.Response(
                        503,
                        json={"error": {"status": "UNAVAILABLE"}},
                        request=request,
                    )
                return httpx.Response(
                    200,
                    json={"items": []},
                    request=request,
                )

            monkeypatch.setattr(
                api_client_module.asyncio,
                "sleep",
                AsyncMock(),
            )
            http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            try:
                await GoogleAPIClient()._send_request(
                    client=http_client,
                    method="GET",
                    url=(
                        "https://calendar.googleapis.com/calendar/v3/"
                        "calendars/primary/events"
                    ),
                    headers={"Authorization": "Bearer test"},
                    params=None,
                    json_data=None,
                    max_retries=1,
                )
            finally:
                await http_client.aclose()
            return metrics, attempts
        finally:
            current_request_metrics.reset(token)

    metrics, attempts = asyncio.run(run())
    assert attempts == 2
    assert metrics.google_calendar_api_call_count == 2
    assert metrics.retry_backoff_count == 1
