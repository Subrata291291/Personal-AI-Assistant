from uuid import UUID
from typing import Literal
from urllib.parse import urlencode, urlsplit

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
from app.services.user_connection_service import UserConnectionService

router = APIRouter()
logger = get_logger(__name__)


def _frontend_connector_url(status_value: str) -> str:
    """Build a safe redirect URL using the trusted frontend configuration."""

    if status_value not in {"connected", "error"}:
        raise ValueError("Invalid Google OAuth callback status.")

    frontend_url = settings.google_oauth_frontend_url
    parsed_frontend_url = urlsplit(frontend_url)

    if (
        parsed_frontend_url.scheme not in {"http", "https"}
        or not parsed_frontend_url.netloc
        or parsed_frontend_url.username
        or parsed_frontend_url.password
        or parsed_frontend_url.path not in {"", "/"}
        or parsed_frontend_url.query
        or parsed_frontend_url.fragment
    ):
        raise ValueError("Invalid configured frontend URL.")

    if (
        settings.APP_ENV.lower() == "production"
        and parsed_frontend_url.scheme != "https"
    ):
        raise ValueError("Production frontend URL must use HTTPS.")

    query = urlencode({
        "google": status_value,
    })

    return f"{frontend_url}/dashboard/connectors?{query}"


@router.post("/google/connect")
def connect_google(
    current_user: CurrentUser,
    db: Session = Depends(get_db),
    service: str = "gmail",
    connection_id: UUID | None = None,
):
    """
    Start Google OAuth authorization for an authenticated user.
    """

    if service in {"drive_read", "drive_file", "drive_write"}:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Google Drive integration is temporarily unavailable and "
                "will be available in a future update."
            ),
        )

    if service not in {
        "gmail",
        "calendar",
        "calendar_write",
        "calendar_list_read",
        "contacts_read",
        "contacts_write",
    }:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unsupported Google service.",
        )

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

        if service in {
            "calendar_write",
            "calendar_list_read",
            "contacts_write",
        }:
            if connection_id is None:
                raise ValueError(
                    "Choose the connected Google account to authorize."
                )
            target_connection = UserConnectionService(db).repository.get_by_id(
                user_id=user_id,
                connection_id=connection_id,
            )
            if (
                target_connection is None
                or target_connection.provider != "google"
                or target_connection.status != "connected"
            ):
                raise ValueError(
                    "The selected Google account is not connected."
                )
        elif connection_id is not None:
            raise ValueError(
                "A target account is supported only for incremental "
                "service consent."
            )

        state_service = OAuthStateService(db)

        state_data = state_service.create_state(
            user_id=user_id,
            provider="google",
            redirect_uri=settings.GOOGLE_REDIRECT_URI,
            service=service,
            target_connection_id=connection_id,
        )

        oauth_client = GoogleOAuthClient()

        authorization_url = oauth_client.build_authorization_url(
            state=state_data["state"],
            code_challenge=state_data["code_challenge"],
            service=service,
        )

        return {
            "provider": "google",
            "authorization_url": authorization_url,
            "expires_at": state_data["expires_at"],
        }

    except KeyError as exc:
        db.rollback()

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unable to identify the authenticated user.",
        ) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except RuntimeError as exc:
        db.rollback()
        # OAuth client/state helpers raise RuntimeError for configuration or
        # upstream preparation failures. Return a safe, actionable message
        # without exposing credentials/tokens.
        raw_message = str(exc).strip()
        message = raw_message or "Unable to prepare Google authorization."
        lower_raw = raw_message.casefold()
        safe_prefixes = (
            "GOOGLE_CLIENT_ID",
            "GOOGLE_REDIRECT_URI",
            "Google OAuth",
            "Invalid Google redirect URI",
            "Production Google redirect URI",
            "Unsupported redirect URI",
            "Google redirect URI",
            "Invalid OAuth state",
            "Invalid PKCE",
        )
        if "encryption_key" in lower_raw or "encrypt" in lower_raw or "decrypt" in lower_raw or "fernet" in lower_raw:
            logger.exception(
                "Unable to initiate Google OAuth due to secure configuration/encryption error."
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "Google OAuth could not be prepared because secure server "
                    "configuration is unavailable. Please verify server "
                    "configuration and try again later."
                ),
            ) from exc
        if not any(
            message.startswith(prefix) or prefix in message
            for prefix in safe_prefixes
        ):
            logger.exception("Unable to initiate Google OAuth (runtime error).")
            message = "Unable to prepare Google authorization. Please try again."
        else:
            logger.warning(
                "Unable to initiate Google OAuth: %s",
                message,
            )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE
            if "not configured" in message.casefold() or "missing" in message.casefold()
            else status.HTTP_400_BAD_REQUEST,
            detail=message,
        ) from exc

    except Exception as exc:
        db.rollback()

        logger.exception("Unable to initiate Google OAuth.")

        detail = "Unable to initiate Google OAuth. Please try again."
        # Surface a safe hint for common setup failures without leaking secrets.
        text = str(exc).casefold()
        if "encrypt" in text or "decrypt" in text or "fernet" in text or "encryption_key" in text:
            detail = (
                "Google OAuth could not be prepared because secure server "
                "configuration is unavailable. Please verify server "
                "configuration and try again later."
            )
        elif "oauth_state" in text or "pa_oauth_states" in text or "database" in text or "connection" in text:
            detail = "Google OAuth could not be prepared right now. Please try again later."
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE
            if "secure server configuration is unavailable" in detail.casefold()
            or "could not be prepared right now" in detail.casefold()
            else status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=detail,
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


@router.get("/connections")
def list_google_connections(
    current_user: CurrentUser,
    db: Session = Depends(get_db),
):
    """
    List external account connections for the authenticated user.
    """

    try:
        user_id = UUID(str(current_user["id"]))
        user_metadata = (
            current_user.get("user_metadata")
            if isinstance(current_user, dict)
            else None
        )
        if not isinstance(user_metadata, dict):
            user_metadata = {}

        service = UserConnectionService(db)

        connections = service.list_connections(
            user_id=user_id,
        )
        calendar_capabilities = service.aggregate_calendar_capabilities(
            connections,
        )
        contacts_capabilities = service.aggregate_contacts_capabilities(
            connections,
        )
        contacts_access = service.get_user_contacts_access(
            user_id=user_id,
            default_connection_id=user_metadata.get(
                "default_contacts_connection_id"
            ),
        )
        contacts_capabilities.update({
            "read_available": bool(contacts_access.get("read_connection_id")),
            "chat_available": bool(contacts_access.get("read_connection_id")),
            "selection_required": bool(contacts_access.get("selection_required")),
            "default_connection_invalid": bool(
                contacts_access.get("default_connection_invalid")
            ),
            "default_connection_selected": bool(
                contacts_access.get("default_connection_selected")
            ),
            "read_scope_missing": bool(
                contacts_access.get("read_scope_missing")
            ),
            "selected_connection_email": contacts_access.get(
                "selected_connection_email"
            ),
            "candidate_emails": contacts_access.get("candidate_emails", []),
        })
        drive_capabilities = service.aggregate_drive_capabilities(
            connections,
        )

        return {
            "connections": connections,
            "total": len(connections),
            "calendar_capabilities": calendar_capabilities,
            "contacts_capabilities": contacts_capabilities,
            "drive_capabilities": drive_capabilities,
        }

    except (KeyError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unable to identify the authenticated user.",
        ) from exc

    except Exception as exc:
        logger.exception("Unable to retrieve user connections.")

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to retrieve connections.",
        ) from exc


@router.delete("/connections/{connection_id}")
def disconnect_account(
    connection_id: UUID,
    current_user: CurrentUser,
    db: Session = Depends(get_db),
):
    """
    Disconnect an external account owned by the authenticated user.
    """

    try:
        user_id = UUID(str(current_user["id"]))

        service = UserConnectionService(db)

        deleted = service.disconnect_account(
            user_id=user_id,
            connection_id=connection_id,
        )

        if not deleted:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Connection not found.",
            )

        return {
            "message": "Account disconnected successfully.",
            "connection_id": str(connection_id),
        }

    except (KeyError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unable to identify the authenticated user.",
        ) from exc

    except HTTPException:
        raise

    except Exception as exc:
        logger.exception("Unable to disconnect account.")

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to disconnect account.",
        ) from exc