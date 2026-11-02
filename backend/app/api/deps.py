
from typing import Annotated, Any

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.supabase_auth import verify_supabase_token
from app.integrations.supabase_client import create_user_supabase_client


bearer_scheme = HTTPBearer(auto_error=False)


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

    user = await verify_supabase_token(credentials.credentials)

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
