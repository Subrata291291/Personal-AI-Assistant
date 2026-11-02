from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser
from app.db.session import get_db
from app.schemas.user_settings import (
    UserSettingsResponse,
    UserSettingsUpdate,
)
from app.services.user_settings_service import UserSettingsService


router = APIRouter()


def get_user_settings_service(
    db: Annotated[Session, Depends(get_db)],
) -> UserSettingsService:
    return UserSettingsService(db)


UserSettingsServiceDependency = Annotated[
    UserSettingsService,
    Depends(get_user_settings_service),
]


@router.get("/me")
def get_my_profile(user: CurrentUser):
    """
    Return the currently authenticated Supabase user.
    """

    return {
        "user_id": str(user["id"]),
        "email": user.get("email"),
        "role": user.get("role"),
    }


@router.get(
    "/me/settings",
    response_model=UserSettingsResponse,
)
def get_my_settings(
    user: CurrentUser,
    service: UserSettingsServiceDependency,
):
    """
    Retrieve or initialize the authenticated user's settings.
    """

    try:
        return service.get_settings(
            user_id=UUID(str(user["id"])),
        )

    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to retrieve user settings.",
        )


@router.patch(
    "/me/settings",
    response_model=UserSettingsResponse,
)
def update_my_settings(
    payload: UserSettingsUpdate,
    user: CurrentUser,
    service: UserSettingsServiceDependency,
):
    """
    Update the authenticated user's memory preference.
    """

    try:
        return service.update_memory_enabled(
            user_id=UUID(str(user["id"])),
            memory_enabled=payload.memory_enabled,
        )

    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )

    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to update user settings.",
        )