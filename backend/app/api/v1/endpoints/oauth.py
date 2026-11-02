from uuid import UUID
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser
from app.core.config import settings
from app.core.logging import get_logger
from app.db.session import get_db
from app.integrations.google.oauth_client import GoogleOAuthClient
from app.services.google_oauth_callback_service import (
    GoogleOAuthCallbackService,
)
from app.services.oauth_state_service import OAuthStateService


router = APIRouter()
logger = get_logger(__name__)


def _frontend_connector_url(status_value: str) -> str:
    """Build a redirect URL using the trusted frontend configuration."""

    frontend_url = settings.FRONTEND_URL.rstrip("/")

    query = urlencode({
        "google": status_value,
    })

    return f"{frontend_url}/connectors?{query}"


@router.post("/google/connect")
def connect_google(
    current_user: CurrentUser,
    db: Session = Depends(get_db),
):
    """
    Start Google OAuth authorization for an authenticated user.
    """

    if not settings.GOOGLE_CLIENT_ID:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google OAuth is not configured.",
        )

    if not settings.GOOGLE_REDIRECT_URI:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google OAuth redirect URI is not configured.",
        )

    try:
        user_id = UUID(str(current_user["id"]))

        state_service = OAuthStateService(db)

        state_data = state_service.create_state(
            user_id=user_id,
            provider="google",
            redirect_uri=settings.GOOGLE_REDIRECT_URI,
        )

        oauth_client = GoogleOAuthClient()

        authorization_url = oauth_client.build_authorization_url(
            state=state_data["state"],
            code_challenge=state_data["code_challenge"],
        )

        return {
            "provider": "google",
            "authorization_url": authorization_url,
            "expires_at": state_data["expires_at"],
        }

    except (KeyError, ValueError) as exc:
        db.rollback()

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unable to identify the authenticated user.",
        ) from exc

    except Exception as exc:
        db.rollback()

        logger.error(
            "Unable to initiate Google OAuth."
        )

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to initiate Google OAuth.",
        ) from exc


@router.get("/google/callback")
async def google_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
):
    """
    Handle Google's OAuth callback.

    Google redirects here with authorization code and state.
    The initiating user is recovered from the one-time state record.

    OAuth tokens are never returned to the frontend.
    """

    if not state:
        logger.warning(
            "Google OAuth callback received without state."
        )

        return RedirectResponse(
            url=_frontend_connector_url("error"),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    try:
        callback_service = GoogleOAuthCallbackService(db)

        result = await callback_service.handle_callback(
            code=code,
            state=state,
            error=error,
        )

        if result.get("status") != "connected":
            raise RuntimeError(
                "Google connection was not completed."
            )

        logger.info(
            "Google account connection completed."
        )

        return RedirectResponse(
            url=_frontend_connector_url("connected"),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    except ValueError:
        db.rollback()

        logger.warning(
            "Google OAuth callback validation failed."
        )

        return RedirectResponse(
            url=_frontend_connector_url("error"),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    except Exception:
        db.rollback()

        logger.error(
            "Google OAuth callback processing failed."
        )

        return RedirectResponse(
            url=_frontend_connector_url("error"),
            status_code=status.HTTP_303_SEE_OTHER,
        )
