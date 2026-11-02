import asyncio
from unittest.mock import AsyncMock, patch

import httpx

from app.integrations.google.api_client import GoogleAPIClient
from app.integrations.google.exceptions import GoogleAPIError


class MockHTTPClient:

    def __init__(self):
        self.call_count = 0

    async def request(self, **kwargs):
        self.call_count += 1
        raise httpx.TimeoutException("Simulated timeout")


async def verify_timeout_retry():

    client = GoogleAPIClient()
    mock_client = MockHTTPClient()

    with patch(
        "app.integrations.google.api_client.asyncio.sleep",
        new_callable=AsyncMock,
    ) as mock_sleep:

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
    assert mock_sleep.await_count == 3

    print("Network timeout retry limit: PASSED")


async def main():

    await verify_timeout_retry()

    print("\nGoogle API network error verification: PASSED")


if __name__ == "__main__":
    asyncio.run(main())