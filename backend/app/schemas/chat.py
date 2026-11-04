
from uuid import UUID
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, ConfigDict, field_validator


class ChatRequest(BaseModel):
    conversation_id: UUID
    content: str = Field(min_length=1, max_length=20000)
    gmail_connection_id: UUID | None = None
    user_timezone: str = Field(default="UTC", min_length=1, max_length=64)

    @field_validator("user_timezone")
    @classmethod
    def validate_user_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("A valid IANA timezone is required.") from exc
        return value


class ChatMessageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    conversation_id: UUID
    role: str
    content: str
    metadata: dict | None = None
    created_at: datetime


class ChatResponse(BaseModel):
    conversation_id: UUID
    user_message: ChatMessageResponse
    assistant_message: ChatMessageResponse
    provider: str
    model: str
