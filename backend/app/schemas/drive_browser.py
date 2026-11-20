"""Normalized response schemas for the read-only Drive browser.

These models deliberately expose only safe file metadata. Provider payloads,
OAuth credentials, tokens, and document content never appear in this module.
Fields use the project's snake_case convention and are mapped from the Google
Drive camelCase metadata by ``DriveBrowserService``.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

# Google Drive file IDs are URL-safe base64-like identifiers.
DRIVE_FILE_ID_PATTERN = r"^[A-Za-z0-9_-]{3,200}$"

# Maximum length accepted for an opaque Google page token. The token itself is
# validated by the service; this only bounds the transport layer.
MAX_PAGE_TOKEN_LENGTH = 4096


class DriveFile(BaseModel):
    """Normalized metadata for a single Drive file or folder."""

    id: str = Field(pattern=DRIVE_FILE_ID_PATTERN)
    name: str
    mime_type: str
    modified_time: datetime | None = None
    created_time: datetime | None = None
    size: int | None = None
    parents: list[str] = Field(default_factory=list)
    web_view_link: str | None = None
    icon_link: str | None = None
    trashed: bool = False
    can_download: bool = False
    can_edit: bool = False
    is_folder: bool = False


class DriveFolder(DriveFile):
    """A Drive folder. Folder items are instantiated with this model."""

    is_folder: Literal[True] = True


class DriveFileListResponse(BaseModel):
    """A single page of Drive browser results with explicit pagination state."""

    items: list[DriveFile]
    next_page_token: str | None = None
    page_size: int
    folder_id: str | None = None
    query: str | None = None
    order_by: str


class DriveBrowserStatusResponse(BaseModel):
    """Availability and connection capability summary (no credentials)."""

    available: bool
    reason: str | None = None
    feature_enabled: bool
    integration_enabled: bool
    connected: bool
    read_scope_granted: bool
    selection_required: bool = False
    connection_id: UUID | None = None
    connection_email: str | None = None
