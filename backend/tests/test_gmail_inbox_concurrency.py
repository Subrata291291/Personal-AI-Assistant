import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest

from app.integrations.google.api_client import (
    GmailReadContext,
    GoogleAPIClient,
)
from app.integrations.google.exceptions import GoogleAPIError
from app.services.gmail_service import GmailService


def _metadata(message_id):
    return {
        "id": message_id,
        "threadId": f"thread-{message_id}",
        "snippet": f"Preview {message_id}",
        "internalDate": message_id,
        "labelIds": ["INBOX"],
        "payload": {
            "headers": [
                {"name": "From", "value": f"Sender {message_id}"},
                {"name": "To", "value": f"Recipient {message_id}"},
                {"name": "Subject", "value": f"Subject {message_id}"},
                {"name": "Date", "value": f"Date {message_id}"},
            ],
        },
    }


def _prepare_service(service, monkeypatch, google_get):
    client = object()
    read_context = GmailReadContext(
        user_id=uuid4(),
        connection_id=uuid4(),
        access_token="test-access-token",
    )
    session_count = 0

    @asynccontextmanager
    async def fake_api_session():
        nonlocal session_count
        session_count += 1
        yield client

    async def fake_read_context(*_args):
        return read_context

    monkeypatch.setattr(service, "_validate_connection", lambda *_args: None)
    monkeypatch.setattr(
        service,
        "_create_gmail_read_context",
        fake_read_context,
    )
    monkeypatch.setattr(service, "_google_get", google_get)
    monkeypatch.setattr(service.api_client, "gmail_session", fake_api_session)
    return client, read_context, lambda: session_count


def test_inbox_metadata_is_ordered_and_keeps_required_fields(monkeypatch):
    service = GmailService(db=None)
    requests = []

    async def fake_google_get(**kwargs):
        requests.append(kwargs)
        if kwargs["endpoint"] == "messages":
            return {
                "messages": [
                    {"id": "message-2", "threadId": "thread-2"},
                    {"id": "message-1", "threadId": "thread-1"},
                ],
                "nextPageToken": "next-page",
                "resultSizeEstimate": 2,
            }
        return _metadata(kwargs["endpoint"].split("/", 1)[1])

    client, read_context, session_count = _prepare_service(
        service,
        monkeypatch,
        fake_google_get,
    )
    result = asyncio.run(service.list_messages(
        user_id=uuid4(),
        connection_id=uuid4(),
        max_results=10,
        folder="sent",
        query="from:sender",
    ))

    assert [item["id"] for item in result["messages"]] == [
        "message-2",
        "message-1",
    ]
    assert result["next_page_token"] == "next-page"
    assert result["messages"][0] == {
        "id": "message-2",
        "thread_id": "thread-message-2",
        "snippet": "Preview message-2",
        "sender": "Sender message-2",
        "recipient": "Recipient message-2",
        "subject": "Subject message-2",
        "internal_date": "message-2",
        "date": "Date message-2",
        "labels": ["INBOX"],
    }
    assert requests[0]["params"] == {
        "maxResults": 10,
        "q": "in:sent from:sender",
    }
    assert requests[1]["params"]["format"] == "metadata"
    assert requests[1]["params"]["metadataHeaders"] == [
        "From",
        "To",
        "Subject",
        "Date",
    ]
    assert requests[1]["allow_concurrent_read"] is True
    assert requests[0]["read_context"] is read_context
    assert all(
        request["read_context"] is read_context
        and request["http_client"] is client
        for request in requests
    )
    assert session_count() == 1


def test_metadata_failure_still_propagates(monkeypatch):
    service = GmailService(db=None)
    failure = RuntimeError("metadata request failed")

    async def fake_google_get(**kwargs):
        if kwargs["endpoint"] == "messages":
            return {
                "messages": [{
                    "id": "message-1",
                    "threadId": "thread-1",
                }],
                "resultSizeEstimate": 1,
            }
        raise failure

    _prepare_service(service, monkeypatch, fake_google_get)

    with pytest.raises(RuntimeError, match="metadata request failed"):
        asyncio.run(service.list_messages(
            user_id=uuid4(),
            connection_id=uuid4(),
            max_results=10,
        ))


def test_inbox_metadata_concurrency_is_bounded_and_preserves_order(
    monkeypatch,
):
    service = GmailService(db=None)
    limit_reached = asyncio.Event()
    release = asyncio.Event()
    active = 0
    maximum_active = 0

    async def fake_google_get(**kwargs):
        nonlocal active, maximum_active
        if kwargs["endpoint"] == "messages":
            return {
                "messages": [
                    {
                        "id": f"message-{index}",
                        "threadId": f"thread-{index}",
                    }
                    for index in range(1, 8)
                ],
                "resultSizeEstimate": 7,
            }

        active += 1
        maximum_active = max(maximum_active, active)
        if active == service.MAX_CONCURRENT_LIST_METADATA_READS:
            limit_reached.set()
        try:
            await release.wait()
            return _metadata(kwargs["endpoint"].split("/", 1)[1])
        finally:
            active -= 1

    _prepare_service(service, monkeypatch, fake_google_get)

    async def run_test():
        task = asyncio.create_task(service.list_messages(
            user_id=uuid4(),
            connection_id=uuid4(),
            max_results=7,
        ))
        await asyncio.wait_for(limit_reached.wait(), timeout=1)
        assert maximum_active == service.MAX_CONCURRENT_LIST_METADATA_READS
        release.set()
        return await task

    result = asyncio.run(run_test())
    assert maximum_active == service.MAX_CONCURRENT_LIST_METADATA_READS
    assert [item["id"] for item in result["messages"]] == [
        f"message-{index}" for index in range(1, 8)
    ]


def test_independent_inbox_requests_have_independent_semaphores(monkeypatch):
    service = GmailService(db=None)
    both_started = asyncio.Event()
    release = asyncio.Event()
    active = 0
    maximum_active = 0

    async def fake_google_get(**kwargs):
        nonlocal active, maximum_active
        if kwargs["endpoint"] == "messages":
            return {
                "messages": [{
                    "id": str(kwargs["user_id"]),
                    "threadId": f"thread-{kwargs['user_id']}",
                }],
                "resultSizeEstimate": 1,
            }

        active += 1
        maximum_active = max(maximum_active, active)
        if active == 2:
            both_started.set()
        try:
            await release.wait()
            return _metadata(kwargs["endpoint"].split("/", 1)[1])
        finally:
            active -= 1

    _prepare_service(service, monkeypatch, fake_google_get)

    async def run_test():
        user_a, user_b = uuid4(), uuid4()
        request_a = asyncio.create_task(service.list_messages(
            user_id=user_a,
            connection_id=uuid4(),
            max_results=1,
        ))
        request_b = asyncio.create_task(service.list_messages(
            user_id=user_b,
            connection_id=uuid4(),
            max_results=1,
        ))
        await asyncio.wait_for(both_started.wait(), timeout=1)
        release.set()
        results = await asyncio.gather(request_a, request_b)
        return user_a, user_b, results

    user_a, user_b, results = asyncio.run(run_test())
    assert maximum_active == 2
    assert results[0]["messages"][0]["id"] == str(user_a)
    assert results[1]["messages"][0]["id"] == str(user_b)


@pytest.mark.parametrize(
    "response",
    [
        None,
        {},
        {"messages": {}, "resultSizeEstimate": 0},
        {"messages": [{"id": ""}], "resultSizeEstimate": 1},
        {"messages": [{"id": 123}], "resultSizeEstimate": 1},
        {"messages": [{"id": "message-1"}], "resultSizeEstimate": 1},
        {"messages": [{"id": "message-1", "threadId": 123}],
         "resultSizeEstimate": 1},
        {"messages": [], "resultSizeEstimate": -1},
        {"messages": [], "resultSizeEstimate": True},
        {"messages": [], "resultSizeEstimate": 0, "nextPageToken": 123},
        {"messages": [], "resultSizeEstimate": 0, "nextPageToken": ""},
        {"messages": [], "resultSizeEstimate": 0, "nextPageToken": None},
        {"resultSizeEstimate": 1},
        {
            "resultSizeEstimate": 0,
            "nextPageToken": "unexpected-next-page",
        },
    ],
)
def test_malformed_gmail_list_responses_are_rejected(monkeypatch, response):
    service = GmailService(db=None)

    async def fake_google_get(**_kwargs):
        return response

    _prepare_service(service, monkeypatch, fake_google_get)

    with pytest.raises(GoogleAPIError) as error:
        asyncio.run(service.list_messages(
            user_id=uuid4(),
            connection_id=uuid4(),
        ))

    assert error.value.status_code == 502


def test_gmail_list_accepts_empty_messages_array(monkeypatch):
    service = GmailService(db=None)

    async def fake_google_get(**_kwargs):
        return {"messages": [], "resultSizeEstimate": 0}

    _prepare_service(service, monkeypatch, fake_google_get)

    result = asyncio.run(service.list_messages(
        user_id=uuid4(),
        connection_id=uuid4(),
    ))

    assert result == {
        "messages": [],
        "next_page_token": None,
        "result_size_estimate": 0,
    }


def test_gmail_list_accepts_omitted_messages_for_confirmed_empty_result(
    monkeypatch,
):
    service = GmailService(db=None)

    async def fake_google_get(**_kwargs):
        return {"resultSizeEstimate": 0}

    _prepare_service(service, monkeypatch, fake_google_get)

    result = asyncio.run(service.list_messages(
        user_id=uuid4(),
        connection_id=uuid4(),
    ))

    assert result["messages"] == []
    assert result["result_size_estimate"] == 0


def test_gmail_list_provider_error_propagates(monkeypatch):
    service = GmailService(db=None)
    failure = GoogleAPIError(503, "provider unavailable", retryable=True)

    async def fake_google_get(**_kwargs):
        raise failure

    _prepare_service(service, monkeypatch, fake_google_get)

    with pytest.raises(GoogleAPIError) as error:
        asyncio.run(service.list_messages(
            user_id=uuid4(),
            connection_id=uuid4(),
        ))

    assert error.value is failure


def test_gmail_list_timeout_becomes_retryable_service_error(monkeypatch):
    service = GmailService(db=None)
    assert service.LIST_MESSAGES_TIMEOUT < 30
    service.LIST_MESSAGES_TIMEOUT = 0.01

    async def stalled_google_get(**_kwargs):
        await asyncio.Future()

    _prepare_service(service, monkeypatch, stalled_google_get)

    with pytest.raises(GoogleAPIError) as error:
        asyncio.run(service.list_messages(
            user_id=uuid4(),
            connection_id=uuid4(),
        ))

    assert error.value.status_code == 503
    assert error.value.retryable is True
    assert error.value.message == "Gmail message list request timed out."


def test_gmail_network_errors_stop_at_the_configured_retry_limit(monkeypatch):
    client = GoogleAPIClient()
    request = httpx.Request(
        "GET",
        "https://gmail.googleapis.com/gmail/v1/users/me/messages",
    )
    failure = httpx.ConnectError("connection failed", request=request)
    http_client = Mock()
    http_client.request = AsyncMock(
        side_effect=[failure, failure],
    )

    async def no_wait(_delay):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_wait)

    with pytest.raises(GoogleAPIError) as error:
        asyncio.run(
            client._send_request(
                client=http_client,
                method="GET",
                url=str(request.url),
                headers={},
                params=None,
                json_data=None,
                max_retries=1,
            )
        )

    assert error.value.status_code == 503
    assert error.value.retryable is True
    assert http_client.request.await_count == 2


def test_gmail_request_uses_httpx_trace_extension_with_mock_transport():
    client = GoogleAPIClient()

    async def run_request():
        async def handler(_request):
            trace = _request.extensions["trace"]
            await trace("connection.connect_tcp.started", {})
            await trace("connection.connect_tcp.complete", {})
            return httpx.Response(200, json={"messages": []})

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
        ) as http_client:
            return await client._send_request(
                client=http_client,
                method="GET",
                url=(
                    "https://gmail.googleapis.com/gmail/v1/users/me/"
                    "messages"
                ),
                headers={},
                params=None,
                json_data=None,
                max_retries=1,
            )

    assert asyncio.run(run_request()) == {"messages": []}
