from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class MemoryReviewResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    existing_memory_id: UUID | None
    existing_content: str
    proposed_content: str
    reason: str
    confidence: float = Field(ge=0, le=1)
    status: str
    resolved_at: datetime | None
    created_at: datetime