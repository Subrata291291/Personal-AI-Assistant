import asyncio
import logging
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from sqlalchemy.orm import Session

from app.core.request_metrics import record_timing, request_id
from app.repositories.oauth_credential_repository import (
    OAuthCredentialRepository,
)
from app.repositories.user_connection_repository import (
    UserConnectionRepository,
)
from app.integrations.google.exceptions import (
    GoogleAPIError,
    GoogleAPIMutationError,
    GoogleOAuthAuthorizationError,
)
from app.services.oauth_credential_service import OAuthCredentialService


logger = logging.getLogger(__name__)


@dataclass
class GmailReadContext:
    user_id: UUID
    connection_id: UUID
    access_token: str = field(repr=False)
    refresh_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock,
        repr=False,
    )


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
    SAFE_GOOGLE_ERROR_REASON = re.compile(r"[A-Za-z0-9_.-]{1,80}")

    def __init__(self):
        self.timeout = self.REQUEST_TIMEOUT

    async def create_gmail_read_context(
        self,
        db: Session,
        user_id: UUID,
        connection_id: UUID,
    ) -> GmailReadContext:
        credential_service = OAuthCredentialService(db)
        try:
            access_token = await credential_service.get_valid_access_token(
                user_id=user_id,
                connection_id=connection_id,
            )
            db.commit()
        except GoogleOAuthAuthorizationError as exc:
            self._clear_revoked_connection_credentials(
                db,
                user_id,
                connection_id,
            )
            raise exc
        except Exception:
            db.rollback()
            raise

        return GmailReadContext(
            user_id=user_id,
            connection_id=connection_id,
            access_token=access_token,
        )

    @asynccontextmanager
    async def session(self):
        async with httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=False,
        ) as client:
            yield client

    @asynccontextmanager
    async def gmail_session(self):
        async with httpx.AsyncClient(
            timeout=self.timeout,
            limits=httpx.Limits(
                max_connections=10,
                max_keepalive_connections=5,
            ),
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

    @staticmethod
    def _clear_revoked_connection_credentials(
        db: Session,
        user_id: UUID,
        connection_id: UUID,
    ) -> None:
        db.rollback()
        try:
            disconnected = UserConnectionRepository(db).mark_disconnected(
                user_id=user_id,
                connection_id=connection_id,
            )
            if disconnected:
                OAuthCredentialRepository(db).delete(
                    user_id=user_id,
                    connection_id=connection_id,
                )
            db.commit()
        except Exception as persistence_error:
            db.rollback()
            logger.exception(
                "google_connection_revocation_persist_failed | "
                "user_id=%s | connection_id=%s",
                user_id,
                connection_id,
            )
            raise RuntimeError(
                "Google authorization expired, but connection status "
                "could not be updated."
            ) from persistence_error

    @asynccontextmanager
    async def _active_connection_request(
        self,
        db: Session,
        user_id: UUID,
        connection_id: UUID,
        serialize: bool = True,
    ):
        """Verify connection ownership and optionally serialize provider I/O."""
        connection_repository = UserConnectionRepository(db)
        lock_started_at = perf_counter()
        if serialize:
            connection = (
                connection_repository.get_active_google_connection_for_update(
                    user_id=user_id,
                    connection_id=connection_id,
                )
            )
        else:
            connection = connection_repository.get_active_google_connection(
                user_id=user_id,
                connection_id=connection_id,
            )
        if serialize:
            record_timing(
                "connection_lock_acquisition",
                (perf_counter() - lock_started_at) * 1000,
            )
        if connection is None:
            db.rollback()
            raise ValueError("Connected Google account was not found.")

        credential = OAuthCredentialRepository(db).get_by_connection(
            user_id=user_id,
            connection_id=connection_id,
        )
        if credential is None:
            db.rollback()
            raise ValueError("Google OAuth credentials were not found.")

        try:
            yield
            db.commit()
        except Exception:
            db.rollback()
            raise

    @staticmethod
    def _is_concurrent_read(method: str, url: str) -> bool:
        """Classify the narrow set of provider reads allowed concurrently."""
        parsed = urlsplit(url)
        hostname = parsed.hostname
        path = parsed.path

        if hostname in {"gmail.googleapis.com"}:
            return method == "GET" and path.startswith("/gmail/v1/")
        if hostname in {"calendar.googleapis.com"}:
            if method == "GET":
                return True
            return method == "POST" and path.endswith("/freeBusy")
        if hostname == "www.googleapis.com":
            if method == "GET":
                return path.startswith("/calendar/v3/")
            return (
                method == "POST"
                and path.startswith("/calendar/v3/")
                and path.endswith("/freeBusy")
            )
        return False

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

    @classmethod
    def _error_reason(cls, response: httpx.Response) -> str | None:
        try:
            body = response.json()
        except ValueError:
            return None
        if not isinstance(body, dict):
            return None
        error = body.get("error")
        if not isinstance(error, dict):
            return None
        candidates = [error.get("status")]
        errors = error.get("errors")
        if isinstance(errors, list):
            candidates.extend(
                item.get("reason")
                for item in errors
                if isinstance(item, dict)
            )
        return next(
            (
                reason
                for reason in candidates
                if isinstance(reason, str)
                and cls.SAFE_GOOGLE_ERROR_REASON.fullmatch(reason)
            ),
            None,
        )

    async def _send_request(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        headers: dict[str, str],
        params: dict[str, Any] | None,
        json_data: dict[str, Any] | None,
        max_retries: int | None = None,
    ) -> dict[str, Any]:

        retry_limit = (
            self.MAX_RETRIES
            if max_retries is None
            else max_retries
        )
        if not 0 <= retry_limit <= self.MAX_RETRIES:
            raise ValueError("Invalid Google API retry limit.")

        for attempt in range(retry_limit + 1):

            try:
                started_at = perf_counter()
                response = None
                try:
                    extensions: dict[str, Any] = {}
                    if urlsplit(url).hostname == "gmail.googleapis.com":
                        connect_started_at: float | None = None

                        async def trace(event_name, _info):
                            nonlocal connect_started_at
                            if event_name == "connection.connect_tcp.started":
                                connect_started_at = perf_counter()
                            elif event_name in {
                                "connection.connect_tcp.complete",
                                "connection.connect_tcp.failed",
                            } and connect_started_at is not None:
                                connect_duration_ms = (
                                    perf_counter() - connect_started_at
                                ) * 1000
                                record_timing(
                                    "gmail_connection_acquisition",
                                    connect_duration_ms,
                                )
                                logger.info(
                                    "gmail_connection_acquisition | "
                                    "request_id=%s | duration_ms=%.2f | "
                                    "result=%s",
                                    request_id(),
                                    connect_duration_ms,
                                    event_name.rsplit(".", 1)[-1],
                                )
                                connect_started_at = None

                        extensions["trace"] = trace

                    response = await client.request(
                        method=method,
                        url=url,
                        headers=headers,
                        params=params,
                        json=json_data,
                        extensions=extensions,
                    )
                finally:
                    duration_ms = (perf_counter() - started_at) * 1000
                    provider_host = urlsplit(url).hostname
                    provider_path = urlsplit(url).path
                    if (
                        provider_host == "calendar.googleapis.com"
                        or (
                            provider_host == "www.googleapis.com"
                            and provider_path.startswith("/calendar/v3/")
                        )
                    ):
                        record_timing("google_calendar_api", duration_ms)
                    elif provider_host == "gmail.googleapis.com":
                        record_timing("gmail_api", duration_ms)
                    logger.info(
                        "google_api_request_completed | request_id=%s "
                        "| method=%s | duration_ms=%.2f | status=%s "
                        "| attempt=%s",
                        request_id(),
                        method,
                        duration_ms,
                        (
                            response.status_code
                            if response is not None
                            else "network_error"
                        ),
                        attempt + 1,
                    )

            except asyncio.CancelledError:
                logger.warning(
                    "google_api_request_cancelled | request_id=%s "
                    "| method=%s | attempt=%s",
                    request_id(),
                    method,
                    attempt + 1,
                )
                raise

            except (httpx.TimeoutException, httpx.NetworkError) as exc:

                if (
                    method not in {"GET", "PUT", "DELETE"}
                    or attempt >= retry_limit
                ):
                    logger.warning(
                        "google_api_request_failed | request_id=%s "
                        "| method=%s | attempts=%s | error_category=%s",
                        request_id(),
                        method,
                        attempt + 1,
                        type(exc).__name__,
                    )
                    raise GoogleAPIError(
                        status_code=503,
                        message="Google API network request failed.",
                        retryable=method in {"GET", "PUT", "DELETE"},
                    ) from exc

                logger.warning(
                    "google_api_request_retry | request_id=%s "
                    "| method=%s | retry=%s | error_category=%s",
                    request_id(),
                    method,
                    attempt + 1,
                    type(exc).__name__,
                )
                backoff_started_at = perf_counter()
                backoff_seconds = 0.5 * (2 ** attempt)
                await asyncio.sleep(backoff_seconds)
                record_timing(
                    "retry_backoff",
                    (perf_counter() - backoff_started_at) * 1000,
                )
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
            reason = self._error_reason(response)

            if (
                status_code in self.RETRYABLE_STATUS_CODES
                and method in {"GET", "PUT", "DELETE"}
            ):

                if attempt < retry_limit:
                    retry_after = response.headers.get("Retry-After")

                    try:
                        delay = float(retry_after) if retry_after else (
                            0.5 * (2 ** attempt)
                        )
                    except ValueError:
                        delay = 0.5 * (2 ** attempt)

                    logger.warning(
                        "google_api_request_retry | request_id=%s "
                        "| method=%s | retry=%s | status=%s",
                        request_id(),
                        method,
                        attempt + 1,
                        status_code,
                    )
                    backoff_started_at = perf_counter()
                    await asyncio.sleep(min(max(delay, 0), 10))
                    record_timing(
                        "retry_backoff",
                        (perf_counter() - backoff_started_at) * 1000,
                    )
                    continue

                raise GoogleAPIError(
                    status_code=status_code,
                    message="Google API temporarily unavailable.",
                    retryable=True,
                    reason=reason,
                )

            raise GoogleAPIError(
                status_code=status_code,
                message="Google API request was rejected.",
                retryable=False,
                reason=reason,
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
        allow_concurrent_read: bool = False,
        read_context: GmailReadContext | None = None,
        max_retries: int | None = None,
    ) -> dict[str, Any]:

        self._validate_url(url)

        normalized_user_id = UUID(str(user_id))
        normalized_connection_id = UUID(str(connection_id))

        if not isinstance(method, str):
            raise ValueError("Invalid HTTP method.")

        normalized_method = method.upper()

        if normalized_method not in self.ALLOWED_METHODS:
            raise ValueError("Unsupported HTTP method.")
        if (
            allow_concurrent_read
            and not self._is_concurrent_read(normalized_method, url)
        ):
            raise ValueError(
                "Concurrent connection access is only allowed for Gmail or Calendar reads."
            )
        if read_context is not None and (
            normalized_method != "GET"
            or urlsplit(url).hostname != "gmail.googleapis.com"
            or read_context.user_id != normalized_user_id
            or read_context.connection_id != normalized_connection_id
        ):
            raise ValueError(
                "Gmail read context does not match this request."
            )

        if read_context is not None:
            async with self._client_context(http_client) as client:
                request_access_token = read_context.access_token
                try:
                    return await self._send_request(
                        client=client,
                        method=normalized_method,
                        url=url,
                        headers={
                            "Authorization": (
                                f"Bearer {request_access_token}"
                            ),
                            "Accept": "application/json",
                        },
                        params=params,
                        json_data=json_data,
                        max_retries=max_retries,
                    )
                except GoogleAPIError as exc:
                    if exc.status_code != 401:
                        raise

                async with read_context.refresh_lock:
                    if read_context.access_token == request_access_token:
                        credential_service = OAuthCredentialService(db)
                        try:
                            read_context.access_token = (
                                await credential_service.get_valid_access_token(
                                    user_id=normalized_user_id,
                                    connection_id=normalized_connection_id,
                                    rejected_access_token=request_access_token,
                                )
                            )
                            db.commit()
                        except GoogleOAuthAuthorizationError as exc:
                            self._clear_revoked_connection_credentials(
                                db,
                                normalized_user_id,
                                normalized_connection_id,
                            )
                            raise exc
                        except Exception:
                            db.rollback()
                            raise

                    return await self._send_request(
                        client=client,
                        method=normalized_method,
                        url=url,
                        headers={
                            "Authorization": (
                                f"Bearer {read_context.access_token}"
                            ),
                            "Accept": "application/json",
                        },
                        params=params,
                        json_data=json_data,
                        max_retries=max_retries,
                    )

        credential_service = OAuthCredentialService(db)

        # Step 1: Get a valid access token.
        try:
            access_token = await credential_service.get_valid_access_token(
                user_id=normalized_user_id,
                connection_id=normalized_connection_id,
            )

            db.commit()

        except GoogleOAuthAuthorizationError as exc:
            self._clear_revoked_connection_credentials(
                db,
                normalized_user_id,
                normalized_connection_id,
            )
            raise exc
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
                async with self._active_connection_request(
                    db,
                    normalized_user_id,
                    normalized_connection_id,
                    serialize=not allow_concurrent_read,
                ):
                    return await self._send_request(
                        client=client,
                        method=normalized_method,
                        url=url,
                        headers=headers,
                        params=params,
                        json_data=json_data,
                        max_retries=max_retries,
                    )
            except GoogleAPIError as exc:
                if exc.status_code != 401:
                    raise

                try:
                    new_access_token = (
                        await credential_service.get_valid_access_token(
                            user_id=normalized_user_id,
                            connection_id=normalized_connection_id,
                            rejected_access_token=access_token,
                        )
                    )
                    db.commit()
                except GoogleOAuthAuthorizationError:
                    self._clear_revoked_connection_credentials(
                        db,
                        normalized_user_id,
                        normalized_connection_id,
                    )
                    raise
                except Exception:
                    db.rollback()
                    raise

                headers["Authorization"] = f"Bearer {new_access_token}"
                async with self._active_connection_request(
                    db,
                    normalized_user_id,
                    normalized_connection_id,
                    serialize=not allow_concurrent_read,
                ):
                    return await self._send_request(
                        client=client,
                        method=normalized_method,
                        url=url,
                        headers=headers,
                        params=params,
                        json_data=json_data,
                        max_retries=max_retries,
                    )

    async def request_mutation(
        self,
        db: Session,
        user_id: str,
        connection_id: str,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        json_data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> dict[str, Any]:
        """Send one mutation request without automatic retries."""
        self._validate_url(url)

        normalized_user_id = UUID(str(user_id))
        normalized_connection_id = UUID(str(connection_id))
        if not isinstance(method, str):
            raise ValueError("Invalid Google API mutation method.")
        normalized_method = method.upper()
        if normalized_method not in {"POST", "PUT", "PATCH", "DELETE"}:
            raise ValueError("Unsupported Google API mutation method.")

        mutation_headers = dict(headers or {})
        for name, value in mutation_headers.items():
            if (
                name.lower() != "if-match"
                or not isinstance(value, str)
                or not value.strip()
                or "\r" in value
                or "\n" in value
            ):
                raise ValueError("Unsupported Google API mutation header.")
        if normalized_method in {"PUT", "PATCH", "DELETE"} and not any(
            name.lower() == "if-match" for name in mutation_headers
        ):
            raise ValueError(
                "Conditional If-Match is required for this mutation method."
            )

        credential_service = OAuthCredentialService(db)
        try:
            access_token = await credential_service.get_valid_access_token(
                user_id=normalized_user_id,
                connection_id=normalized_connection_id,
            )
            db.commit()
        except GoogleOAuthAuthorizationError:
            self._clear_revoked_connection_credentials(
                db,
                normalized_user_id,
                normalized_connection_id,
            )
            raise
        except Exception:
            db.rollback()
            raise

        request_headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            **mutation_headers,
        }
        async with self._client_context(http_client) as client:
            async with self._active_connection_request(
                db,
                normalized_user_id,
                normalized_connection_id,
            ):
                return await self._send_mutation_request(
                    client=client,
                    method=normalized_method,
                    url=url,
                    headers=request_headers,
                    params=params,
                    json_data=json_data,
                )

    async def _send_mutation_request(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        headers: dict[str, str],
        params: dict[str, Any] | None,
        json_data: dict[str, Any] | None,
    ) -> dict[str, Any]:
        response = None
        started_at = perf_counter()
        try:
            response = await client.request(
                method=method,
                url=url,
                headers=headers,
                params=params,
                json=json_data,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            raise GoogleAPIMutationError(
                status_code=503,
                message="Google API connection failed before transmission.",
                outcome_unknown=False,
                reason="request_not_transmitted",
            ) from exc
        except (
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.RemoteProtocolError,
        ) as exc:
            raise GoogleAPIMutationError(
                status_code=503,
                message="Google API mutation outcome could not be confirmed.",
                outcome_unknown=True,
                reason="transport_outcome_unknown",
            ) from exc
        finally:
            record_timing("google_mutation_api", (perf_counter() - started_at) * 1000)
            logger.info(
                "google_api_mutation_request_completed | request_id=%s "
                "| method=%s | duration_ms=%.2f | status=%s",
                request_id(),
                method,
                (perf_counter() - started_at) * 1000,
                response.status_code if response is not None else "network_error",
            )

        if response.is_success:
            if response.status_code == 204 or not response.content:
                return {}
            try:
                result = response.json()
            except ValueError as exc:
                raise GoogleAPIMutationError(
                    status_code=response.status_code,
                    message="Google API mutation response could not be confirmed.",
                    outcome_unknown=True,
                    reason="invalid_success_response",
                ) from exc
            if not isinstance(result, dict):
                raise GoogleAPIMutationError(
                    status_code=response.status_code,
                    message="Google API mutation response could not be confirmed.",
                    outcome_unknown=True,
                    reason="invalid_success_response",
                )
            return result

        status_code = response.status_code
        raise GoogleAPIMutationError(
            status_code=status_code,
            message="Google API mutation request was rejected.",
            outcome_unknown=status_code >= 500,
            reason=self._error_reason(response),
        )