import asyncio
import logging
from contextlib import asynccontextmanager
from time import perf_counter
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from sqlalchemy import exc
from sqlalchemy.orm import Session

from app.core.request_metrics import record_timing, request_id
from app.integrations.google.exceptions import GoogleAPIError
from app.services.oauth_credential_service import OAuthCredentialService


logger = logging.getLogger(__name__)


class GoogleAPIClient:

    ALLOWED_HOSTS = {
        "www.googleapis.com",
        "gmail.googleapis.com",
        "calendar.googleapis.com",
        "drive.googleapis.com",
        "people.googleapis.com",
    }

    ALLOWED_METHODS = {
        "GET",
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
    }

    RETRYABLE_STATUS_CODES = {
        429,
        500,
        502,
        503,
        504,
    }

    MAX_RETRIES = 3
    REQUEST_TIMEOUT = 20.0

    def __init__(self):
        self.timeout = self.REQUEST_TIMEOUT

    @asynccontextmanager
    async def session(self):
        async with httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=False,
        ) as client:
            yield client

    @asynccontextmanager
    async def _client_context(self, client: httpx.AsyncClient | None):
        if client is not None:
            yield client
            return
        async with httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=False,
        ) as owned_client:
            yield owned_client

    def _validate_url(self, url: str) -> None:

        if not isinstance(url, str):
            raise ValueError("Invalid Google API URL.")

        parsed = urlsplit(url)

        if parsed.scheme != "https":
            raise ValueError("Google API requests must use HTTPS.")

        if parsed.hostname not in self.ALLOWED_HOSTS:
            raise ValueError("Unapproved Google API host.")

        if parsed.port not in (None, 443):
            raise ValueError("Unapproved Google API port.")

        if parsed.username or parsed.password:
            raise ValueError("Credentials in API URLs are not allowed.")

        if parsed.fragment:
            raise ValueError("URL fragments are not allowed.")

    async def _send_request(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        headers: dict[str, str],
        params: dict[str, Any] | None,
        json_data: dict[str, Any] | None,
    ) -> dict[str, Any]:

        for attempt in range(self.MAX_RETRIES + 1):

            try:
                started_at = perf_counter()
                response = None
                try:
                    response = await client.request(
                        method=method,
                        url=url,
                        headers=headers,
                        params=params,
                        json=json_data,
                    )
                finally:
                    duration_ms = (perf_counter() - started_at) * 1000
                    record_timing("gmail_api", duration_ms)
                    logger.info(
                        "google_api_request_completed | request_id=%s "
                        "| method=%s | duration_ms=%.2f | status=%s",
                        request_id(),
                        method,
                        duration_ms,
                        (
                            response.status_code
                            if response is not None
                            else "network_error"
                        ),
                    )

            except (httpx.TimeoutException, httpx.NetworkError) as exc:

                if (
                    method not in {"GET", "PUT", "DELETE"}
                    or attempt >= self.MAX_RETRIES
                ):
                    raise GoogleAPIError(
                        status_code=503,
                        message="Google API network request failed.",
                        retryable=method in {"GET", "PUT", "DELETE"},
                    ) from exc

                await asyncio.sleep(0.5 * (2 ** attempt))
                continue

            if response.is_success:

                if response.status_code == 204 or not response.content:
                    return {}

                try:
                    result = response.json()
                except ValueError as exc:
                    raise GoogleAPIError(
                        status_code=response.status_code,
                        message="Google API returned invalid JSON.",
                    ) from exc

                if not isinstance(result, dict):
                    raise GoogleAPIError(
                        status_code=response.status_code,
                        message="Unexpected Google API response format.",
                    )

                return result

            status_code = response.status_code

            if (
                status_code in self.RETRYABLE_STATUS_CODES
                and method in {"GET", "PUT", "DELETE"}
            ):

                if attempt < self.MAX_RETRIES:
                    retry_after = response.headers.get("Retry-After")

                    try:
                        delay = float(retry_after) if retry_after else (
                            0.5 * (2 ** attempt)
                        )
                    except ValueError:
                        delay = 0.5 * (2 ** attempt)

                    await asyncio.sleep(min(max(delay, 0), 10))
                    continue

                raise GoogleAPIError(
                    status_code=status_code,
                    message="Google API temporarily unavailable.",
                    retryable=True,
                )

            raise GoogleAPIError(
                status_code=status_code,
                message="Google API request was rejected.",
                retryable=False,
            )

        raise GoogleAPIError(
            status_code=503,
            message="Google API retry limit exceeded.",
            retryable=True,
        )
    

    async def request(
        self,
        db: Session,
        user_id: str,
        connection_id: str,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        json_data: dict[str, Any] | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> dict[str, Any]:

        self._validate_url(url)

        normalized_user_id = UUID(str(user_id))
        normalized_connection_id = UUID(str(connection_id))

        if not isinstance(method, str):
            raise ValueError("Invalid HTTP method.")

        normalized_method = method.upper()

        if normalized_method not in self.ALLOWED_METHODS:
            raise ValueError("Unsupported HTTP method.")

        credential_service = OAuthCredentialService(db)

        # Step 1: Get a valid access token.
        try:
            access_token = await credential_service.get_valid_access_token(
                user_id=normalized_user_id,
                connection_id=normalized_connection_id,
            )

            db.commit()

        except Exception:
            db.rollback()
            raise

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }

        async with self._client_context(http_client) as client:

            try:
                # Step 2: Execute the original request.
                return await self._send_request(
                    client=client,
                    method=normalized_method,
                    url=url,
                    headers=headers,
                    params=params,
                    json_data=json_data,
                )

            except GoogleAPIError as exc:

                # Step 3: Only handle the first 401 response.
                if exc.status_code != 401:
                    raise

                # Step 4: Refresh or reuse a newer access token.
                try:
                    new_access_token = (
                        await credential_service.get_valid_access_token(
                            user_id=normalized_user_id,
                            connection_id=normalized_connection_id,
                            rejected_access_token=access_token,
                        )
                    )

                    db.commit()

                except Exception:
                    db.rollback()
                    raise

                # Step 5: Retry the original request exactly once.
                headers["Authorization"] = (
                    f"Bearer {new_access_token}"
                )

                return await self._send_request(
                    client=client,
                    method=normalized_method,
                    url=url,
                    headers=headers,
                    params=params,
                    json_data=json_data,
                )