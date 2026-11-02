
from typing import Any
from uuid import UUID

import httpx
from fastapi import HTTPException, status

from app.core.config import settings


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

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{supabase_url}/auth/v1/user",
                headers=headers,
            )

    except httpx.TimeoutException as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication service timed out.",
        ) from exc

    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication service is unavailable.",
        ) from exc

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

    return {
        "id": user_id,
        "email": user.get("email"),
        "role": user.get("role"),
        "app_metadata": user.get("app_metadata", {}),
        "user_metadata": user.get("user_metadata", {}),
    }
