import asyncio
from unittest.mock import AsyncMock, patch

import httpx

from app.integrations.google.api_client import GoogleAPIClient
from app.integrations.google.exceptions import GoogleAPIError


class MockHTTPClient:

    def __init__(self, responses):
        self.responses = list(responses)
        self.call_count = 0

    async def request(self, **kwargs):
        self.call_count += 1
        return self.responses.pop(0)


def make_response(status_code, json_data=None):

    return httpx.Response(
        status_code=status_code,
        json=json_data,
        request=httpx.Request("GET", "https://gmail.googleapis.com/test"),
    )


async def verify_retry_after_429():

    client = GoogleAPIClient()

    mock_client = MockHTTPClient([
        make_response(429, {"error": "rate limit"}),
        make_response(200, {"messages": []}),
    ])

    with patch(
        "app.integrations.google.api_client.asyncio.sleep",
        new_callable=AsyncMock,
    ):
        result = await client._send_request(
            mock_client,
            "GET",
            "https://gmail.googleapis.com/test",
            {},
            None,
            None,
        )

    assert result == {"messages": []}
    assert mock_client.call_count == 2

    print("429 retry and recovery: PASSED")


async def verify_no_retry_for_400():

    client = GoogleAPIClient()

    mock_client = MockHTTPClient([
        make_response(400, {"error": "bad request"}),
    ])

    try:
        await client._send_request(
            mock_client,
            "GET",
            "https://gmail.googleapis.com/test",
            {},
            None,
            None,
        )

        raise AssertionError("Expected GoogleAPIError.")

    except GoogleAPIError as exc:
        assert exc.status_code == 400
        assert exc.retryable is False

    assert mock_client.call_count == 1

    print("400 no-retry behaviour: PASSED")


async def verify_retry_limit():

    client = GoogleAPIClient()

    mock_client = MockHTTPClient([
        make_response(503, {"error": "unavailable"}),
        make_response(503, {"error": "unavailable"}),
        make_response(503, {"error": "unavailable"}),
        make_response(503, {"error": "unavailable"}),
    ])

    with patch(
        "app.integrations.google.api_client.asyncio.sleep",
        new_callable=AsyncMock,
    ):
        try:
            await client._send_request(
                mock_client,
                "GET",
                "https://gmail.googleapis.com/test",
                {},
                None,
                None,
            )

            raise AssertionError("Expected GoogleAPIError.")

        except GoogleAPIError as exc:
            assert exc.status_code == 503
            assert exc.retryable is True

    assert mock_client.call_count == 4

    print("503 retry limit: PASSED")


async def main():

    await verify_retry_after_429()
    await verify_no_retry_for_400()
    await verify_retry_limit()

    print("\nGoogle API retry verification: PASSED")


if __name__ == "__main__":
    asyncio.run(main())