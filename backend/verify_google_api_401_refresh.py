import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from app.integrations.google.api_client import GoogleAPIClient
from app.integrations.google.exceptions import GoogleAPIError


USER_ID = "11111111-1111-4111-8111-111111111111"
CONNECTION_ID = "22222222-2222-4222-8222-222222222222"
URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages"


async def verify_401_refresh_success():

    client = GoogleAPIClient()
    db = MagicMock()

    credential_mock = AsyncMock()
    credential_mock.side_effect = ["old-token", "new-token"]

    request_mock = AsyncMock(
        side_effect=[
            GoogleAPIError(401, "Unauthorized"),
            {"messages": []},
        ]
    )

    with (
        patch(
            "app.integrations.google.api_client.OAuthCredentialService.get_valid_access_token",
            credential_mock,
        ),
        patch.object(client, "_send_request", request_mock),
    ):

        result = await client.request(
            db=db,
            user_id=USER_ID,
            connection_id=CONNECTION_ID,
            method="GET",
            url=URL,
        )

    assert result == {"messages": []}
    assert credential_mock.await_count == 2
    assert request_mock.await_count == 2

    second_token_call = credential_mock.await_args_list[1]

    assert second_token_call.kwargs["rejected_access_token"] == "old-token"

    second_request = request_mock.await_args_list[1]

    assert second_request.kwargs["headers"]["Authorization"] == (
        "Bearer new-token"
    )

    assert db.commit.call_count == 2

    print("401 refresh and retry success: PASSED")


async def verify_second_401_stops():

    client = GoogleAPIClient()
    db = MagicMock()

    credential_mock = AsyncMock(
        side_effect=["old-token", "new-token"]
    )

    request_mock = AsyncMock(
        side_effect=[
            GoogleAPIError(401, "Unauthorized"),
            GoogleAPIError(401, "Unauthorized"),
        ]
    )

    with (
        patch(
            "app.integrations.google.api_client.OAuthCredentialService.get_valid_access_token",
            credential_mock,
        ),
        patch.object(client, "_send_request", request_mock),
    ):

        try:
            await client.request(
                db=db,
                user_id=USER_ID,
                connection_id=CONNECTION_ID,
                method="GET",
                url=URL,
            )

            raise AssertionError("Expected GoogleAPIError.")

        except GoogleAPIError as exc:
            assert exc.status_code == 401

    assert credential_mock.await_count == 2
    assert request_mock.await_count == 2

    print("Second 401 stops without another refresh: PASSED")


async def main():

    await verify_401_refresh_success()
    await verify_second_401_stops()

    print("\nGoogle API 401 refresh verification: PASSED")


if __name__ == "__main__":
    asyncio.run(main())