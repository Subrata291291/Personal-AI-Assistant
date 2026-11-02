from datetime import datetime
from uuid import UUID
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
)


ALLOWED_MEMORY_TYPES = {
    "long_term",
    "episodic",
    "semantic",
    "procedural",
}


class MemoryCreate(BaseModel):
    memory_type: str = Field(
        min_length=1,
        max_length=50,
    )

    content: str = Field(
        min_length=1,
        max_length=20000,
    )

    importance: int = Field(
        default=5,
        ge=1,
        le=10,
    )

    confidence: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
    )

    metadata: dict[str, Any] | None = None

    source_conversation_id: UUID | None = None

    expires_at: datetime | None = None


class MemoryUpdate(BaseModel):
    content: str | None = Field(
        default=None,
        min_length=1,
        max_length=20000,
    )

    importance: int | None = Field(
        default=None,
        ge=1,
        le=10,
    )

    confidence: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
    )

    metadata: dict[str, Any] | None = None

    expires_at: datetime | None = None


class MemoryResponse(BaseModel):
    model_config = ConfigDict(
        from_attributes=True,
        populate_by_name=True,
    )

    id: UUID
    user_id: UUID

    memory_type: str
    content: str

    importance: int
    confidence: float

    metadata: dict[str, Any] | None = Field(
        default=None,
        validation_alias="metadata_",
    )

    source_conversation_id: UUID | None = None

    expires_at: datetime | None = None

    created_at: datetime
    updated_at: datetime