from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class UserSettingsUpdate(BaseModel):
    """
    Request schema for updating user settings.
    """

    model_config = ConfigDict(extra="forbid")

    memory_enabled: bool


class UserSettingsResponse(BaseModel):
    """
    Response schema for user settings.
    """

    model_config = ConfigDict(from_attributes=True)

    user_id: UUID
    memory_enabled: bool
    created_at: datetime
    updated_at: datetime