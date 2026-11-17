from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser
from app.core.config import settings
from app.db.session import get_db
from app.integrations.google.exceptions import GoogleAPIError
from app.api.deps import is_integration_enabled
from app.schemas.action_approval import ActionApprovalResponse
from app.services.action_approval_service import ActionApprovalService
from app.services.contacts_idempotency import build_contacts_create_idempotency_context
from app.services.contacts_idempotency_service import (
    ContactsIdempotencyConflictError,
    ContactsIdempotencyService,
)
from app.services.contacts_service import ContactsService
from app.services.contacts_update_verification import (
    ContactsUpdateTargetConflictError,
    verify_and_bind_contacts_update_target,
)
from app.services.user_connection_service import UserConnectionService

router = APIRouter()


def _user_metadata(user: CurrentUser) -> dict[str, Any]:
    metadata = user.get("user_metadata") if isinstance(user, dict) else None
    return metadata if isinstance(metadata, dict) else {}


def _resolve_contacts_read_access(user: CurrentUser, db: Session) -> dict[str, Any]:
    if not settings.CONTACTS_READ_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google Contacts read is currently disabled.",
        )
    user_id = UUID(str(user["id"]))
    metadata = _user_metadata(user)
    access = UserConnectionService(db).get_user_contacts_access(
        user_id=user_id,
        default_connection_id=metadata.get("default_contacts_connection_id"),
    )
    if access.get("selection_required"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "Select a Google account to use Contacts.",
                "code": "contacts_account_selection_required",
                "candidate_emails": access.get("candidate_emails", []),
            },
        )
    if access.get("default_connection_invalid"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "The selected Contacts account is unavailable. Choose a different account.",
                "code": "contacts_account_invalid",
            },
        )
    if not access.get("google_connection_available"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No connected Google account is available for Contacts.",
        )
    read_connection_id = access.get("read_connection_id")
    if not read_connection_id:
        if access.get("read_scope_missing"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "message": "Google Contacts read permission is not granted for the selected account.",
                    "code": "contacts_read_scope_missing",
                },
            )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No eligible Google Contacts connection was found.",
        )
    return {
        "user_id": user_id,
        "connection_id": UUID(str(read_connection_id)),
        "selected_connection_email": access.get("selected_connection_email"),
    }


def _raise_contacts_api_error(exc: Exception) -> None:
    if isinstance(exc, HTTPException):
        raise exc
    if isinstance(exc, PermissionError):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    if isinstance(exc, ValueError):
        message = str(exc)
        lower = message.lower()
        if "not found" in lower:
            code = status.HTTP_404_NOT_FOUND
        elif "invalid" in lower or "required" in lower:
            code = status.HTTP_400_BAD_REQUEST
        else:
            code = status.HTTP_400_BAD_REQUEST
        raise HTTPException(status_code=code, detail=message) from exc
    if isinstance(exc, GoogleAPIError):
        if exc.status_code == 401:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Google authorization is no longer valid. Reconnect Google Contacts in Connected Apps.",
            ) from exc
        if exc.status_code == 403:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Google Contacts permission was denied for the selected account.",
            ) from exc
        if exc.status_code == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="The requested contact was not found.",
            ) from exc
        if exc.status_code == 409:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="The contact changed since it was loaded. Refresh and try again.",
            ) from exc
        if exc.status_code == 429 or exc.status_code >= 500:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Google Contacts is temporarily unavailable. Please try again.",
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Google Contacts could not process the request.",
        ) from exc
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Unable to load Google Contacts.",
    ) from exc


@router.get("")
async def list_contacts(
    user: CurrentUser,
    db: Session = Depends(get_db),
    page_size: int = Query(default=25, ge=1, le=100),
    page_token: str | None = None,
    query: str | None = None,
):
    context = _resolve_contacts_read_access(user, db)
    svc = ContactsService(db)
    try:
        if query and query.strip():
            result = await svc.search_contacts(
                user_id=context["user_id"],
                connection_id=context["connection_id"],
                query=query.strip(),
                page_size=min(page_size, 30),
                page_token=page_token,
            )
            mode = "search"
        else:
            result = await svc.list_contacts(
                user_id=context["user_id"],
                connection_id=context["connection_id"],
                page_size=page_size,
                page_token=page_token,
            )
            mode = "list"
    except Exception as exc:
        _raise_contacts_api_error(exc)
    return {
        "mode": mode,
        "selected_account_email": context["selected_connection_email"],
        "contacts": result.get("contacts", []),
        "next_page_token": result.get("next_page_token"),
        "total_items": result.get("total_items"),
        "total_people": result.get("total_people"),
    }


@router.get("/resolve")
async def resolve_contacts_context(
    user: CurrentUser,
    db: Session = Depends(get_db),
):
    context = _resolve_contacts_read_access(user, db)
    metadata = _user_metadata(user)
    access = UserConnectionService(db).get_user_contacts_access(
        user_id=context["user_id"],
        default_connection_id=metadata.get("default_contacts_connection_id"),
    )
    return {
        "selected_account_email": context["selected_connection_email"],
        "read_available": True,
        "write_available": bool(access.get("write_connection_id")),
        "write_scope_missing": bool(access.get("write_scope_missing")),
        "selection_required": bool(access.get("selection_required")),
        "capabilities": access.get("capabilities", {}),
        "candidate_emails": access.get("candidate_emails", []),
    }


def _resolve_contacts_write_connection(user: CurrentUser, db: Session) -> tuple[UUID, UUID, dict[str, Any]]:
    if not is_integration_enabled(user, "contacts"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Google Contacts is turned off in Connected Apps.",
        )
    user_id = UUID(str(user["id"]))
    metadata = _user_metadata(user)
    access = UserConnectionService(db).get_user_contacts_access(
        user_id=user_id,
        default_connection_id=metadata.get("default_contacts_connection_id"),
    )
    if access.get("selection_required"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "Select a Google account to use Contacts.",
                "code": "contacts_account_selection_required",
                "candidate_emails": access.get("candidate_emails", []),
            },
        )
    if access.get("default_connection_invalid"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "The selected Contacts account is unavailable. Choose a different account.",
                "code": "contacts_account_invalid",
            },
        )
    write_connection_id = access.get("write_connection_id")
    if not write_connection_id:
        if access.get("write_scope_missing"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "message": "Google Contacts write permission is not granted for the selected account.",
                    "code": "contacts_write_scope_missing",
                },
            )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No eligible Google Contacts connection was found for this action.",
        )
    return user_id, UUID(str(write_connection_id)), access


@router.post("/proposals", response_model=ActionApprovalResponse)
async def create_contacts_proposal(
    payload: dict[str, Any],
    user: CurrentUser,
    response: Response,
    db: Session = Depends(get_db),
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    action_type = payload.get("action_type")
    parameters = payload.get("parameters")
    if not isinstance(action_type, str) or not action_type.startswith("contacts."):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A valid Contacts action type is required.",
        )
    if not isinstance(parameters, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Contacts action parameters are required.",
        )

    user_id, connection_id, _access = _resolve_contacts_write_connection(user, db)
    # Enforce feature flags for the specific action.
    if action_type == "contacts.create_contact" and not settings.CONTACTS_CREATE_ENABLED:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Contacts create is currently disabled.")
    if action_type == "contacts.update_contact" and not settings.CONTACTS_UPDATE_ENABLED:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Contacts update is currently disabled.")
    if action_type == "contacts.delete_contact" and not settings.CONTACTS_DELETE_ENABLED:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Contacts delete is currently disabled.")

    if action_type == "contacts.update_contact":
        try:
            verified = await verify_and_bind_contacts_update_target(
                db=db,
                user_id=user_id,
                connection_id=connection_id,
                parameters=parameters,
            )
            parameters = verified.parameters
        except ContactsUpdateTargetConflictError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "message": str(exc),
                    "code": "contacts_update_target_conflict",
                },
            ) from exc
        except GoogleAPIError as exc:
            _raise_contacts_api_error(exc)
        except PermissionError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
        except LookupError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Unable to verify the selected contact before creating an update approval.",
            )

    if action_type == "contacts.create_contact":
        try:
            context = build_contacts_create_idempotency_context(
                user_id=user_id,
                connection_id=connection_id,
                action_type=action_type,
                idempotency_key=idempotency_key,
                parameters=parameters,
            )
            result = ContactsIdempotencyService(db).create_contacts_create_proposal(
                user_id=user_id,
                connection_id=connection_id,
                context=context,
            )
        except ContactsIdempotencyConflictError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
        except LookupError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Unable to create the Contacts approval proposal.",
            )
        response.status_code = status.HTTP_200_OK if result.replayed else status.HTTP_201_CREATED
        return ActionApprovalResponse(proposal=result.proposal)

    try:
        proposal = ActionApprovalService(db).create_standalone_contacts_proposal(
            user_id=user_id,
            connection_id=connection_id,
            action_type=action_type,
            parameters=parameters,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to create the Contacts approval proposal.",
        )
    response.status_code = status.HTTP_201_CREATED
    return ActionApprovalResponse(proposal=proposal)


@router.get("/{resource_name:path}")
async def get_contact(
    resource_name: str,
    user: CurrentUser,
    db: Session = Depends(get_db),
):
    context = _resolve_contacts_read_access(user, db)
    svc = ContactsService(db)
    try:
        result = await svc.get_contact(
            user_id=context["user_id"],
            connection_id=context["connection_id"],
            resource_name=resource_name,
        )
    except Exception as exc:
        _raise_contacts_api_error(exc)
    return {
        "selected_account_email": context["selected_connection_email"],
        "contact": result,
    }