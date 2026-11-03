
from uuid import UUID
from datetime import datetime

from pydantic import BaseModel, Field, ConfigDict


class ChatRequest(BaseModel):
    conversation_id: UUID
    content: str = Field(min_length=1, max_length=20000)
    gmail_connection_id: UUID | None = None


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
