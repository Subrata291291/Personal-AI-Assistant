from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

# Google Drive file IDs are URL-safe base64-like identifiers.
DRIVE_FILE_ID_PATTERN = r"^[A-Za-z0-9_-]{3,200}$"


class DriveSelectedFileCreate(BaseModel):
    file_id: str = Field(
        min_length=3,
        max_length=200,
        pattern=DRIVE_FILE_ID_PATTERN,
    )
    connection_id: UUID | None = None


class DriveSelectedFileResponse(BaseModel):
    provider_file_id: str
    name: str
    mime_type: str
    modified_time: datetime | None = None
    size_bytes: int | None = None
    selected_at: datetime


class DriveSelectedFileListResponse(BaseModel):
    files: list[DriveSelectedFileResponse]
    connection_id: UUID | None = None


class DrivePickerConfigResponse(BaseModel):
    enabled: bool
    api_key: str | None = None
    client_id: str | None = None
    scope: str
    mime_types: list[str]
    reason: str | None = None


class DrivePickerTokenResponse(BaseModel):
    access_token: str
    scope: str
