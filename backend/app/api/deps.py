
import asyncio
import logging
from time import perf_counter
from typing import Annotated, Any

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.supabase_auth import verify_supabase_token
from app.core.request_metrics import record_timing, request_id
from app.integrations.supabase_client import create_user_supabase_client


bearer_scheme = HTTPBearer(auto_error=False)
logger = logging.getLogger(__name__)


async def get_current_user(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(bearer_scheme),
    ],
) -> dict[str, Any]:
    """
    Authenticate the request using a Supabase access token.
    """

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    auth_started_at = perf_counter()
    auth_result = "success"
    error_category = None
    try:
        user = await verify_supabase_token(credentials.credentials)
    except asyncio.CancelledError:
        auth_result = "cancelled"
        error_category = "CancelledError"
        raise
    except Exception as exc:
        auth_result = "error"
        error_category = type(exc).__name__
        raise
    finally:
        duration_ms = (perf_counter() - auth_started_at) * 1000
        record_timing("auth", duration_ms)
        logger.info(
            "authentication_completed | request_id=%s "
            "| duration_ms=%.2f | result=%s",
            request_id(),
            duration_ms,
            auth_result,
        )
        if error_category:
            logger.warning(
                "authentication_failed | request_id=%s "
                "| error_category=%s",
                request_id(),
                error_category,
            )

    # Preserve the verified access token for request-scoped use.
    user["access_token"] = credentials.credentials

    return user


async def get_current_supabase_client(
    user: Annotated[dict[str, Any], Depends(get_current_user)],
):
    """
    Create a Supabase client for the authenticated user.
    """

    return create_user_supabase_client(user["access_token"])


CurrentUser = Annotated[
    dict[str, Any],
    Depends(get_current_user),
]


def is_integration_enabled(
    user: dict[str, Any],
    integration: str,
) -> bool:
    metadata = user.get("user_metadata")
    if not isinstance(metadata, dict):
        return True
    return metadata.get(f"{integration}_enabled") is not False


def require_integration_enabled(integration: str):
    async def dependency(user: CurrentUser) -> None:
        if not is_integration_enabled(user, integration):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"{integration.title()} is turned off in Connected Apps."
                ),
            )

    return dependency
