
import asyncio
from datetime import datetime
import logging
from time import perf_counter
from typing import Any
from uuid import UUID

import httpx
from fastapi import HTTPException, status

from app.core.config import settings
from app.core.request_metrics import record_timing, request_id


logger = logging.getLogger(__name__)


def _has_verified_email(user: dict[str, Any]) -> bool:
    email = user.get("email")
    confirmed_at = user.get("email_confirmed_at")
    if not isinstance(email, str) or not email.strip():
        return False
    if not isinstance(confirmed_at, str) or not confirmed_at.strip():
        return False

    try:
        timestamp = datetime.fromisoformat(
            confirmed_at.strip().replace("Z", "+00:00")
        )
    except ValueError:
        return False

    return timestamp.tzinfo is not None and timestamp.utcoffset() is not None


async def verify_supabase_token(token: str) -> dict[str, Any]:
    """
    Verify a Supabase access token using Supabase Auth.

    Supabase validates the JWT signature, expiration,
    issuer and token validity.

    Returns the authenticated user's information.
    """

    if not token or not token.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing access token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not settings.SUPABASE_URL or not settings.SUPABASE_ANON_KEY:
        raise RuntimeError(
            "Supabase authentication configuration is missing."
        )

    supabase_url = settings.SUPABASE_URL.rstrip("/")
    api_key = settings.SUPABASE_ANON_KEY.get_secret_value()

    headers = {
        "apikey": api_key,
        "Authorization": f"Bearer {token}",
    }

    request_started_at = perf_counter()
    request_result = "success"
    error_category = None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{supabase_url}/auth/v1/user",
                headers=headers,
            )

    except asyncio.CancelledError:
        request_result = "cancelled"
        error_category = "CancelledError"
        raise

    except httpx.TimeoutException as exc:
        request_result = "error"
        error_category = type(exc).__name__
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication service timed out.",
        ) from exc

    except httpx.RequestError as exc:
        request_result = "error"
        error_category = type(exc).__name__
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication service is unavailable.",
        ) from exc
    finally:
        duration_ms = (perf_counter() - request_started_at) * 1000
        record_timing("supabase_auth_http", duration_ms)
        logger.info(
            "supabase_auth_request_completed | request_id=%s "
            "| duration_ms=%.2f | result=%s",
            request_id(),
            duration_ms,
            request_result,
        )
        if error_category:
            logger.warning(
                "supabase_auth_request_failed | request_id=%s "
                "| error_category=%s",
                request_id(),
                error_category,
            )

    if response.status_code in (401, 403):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired access token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if response.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Unable to verify user with authentication service.",
        )

    try:
        user = response.json()
        user_id = UUID(user["id"])
    except (ValueError, KeyError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid user information returned by authentication service.",
        )

    if not _has_verified_email(user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account email verification is required.",
        )

    return {
        "id": user_id,
        "email": user.get("email"),
        "role": user.get("role"),
        "app_metadata": user.get("app_metadata", {}),
        "user_metadata": user.get("user_metadata", {}),
    }
