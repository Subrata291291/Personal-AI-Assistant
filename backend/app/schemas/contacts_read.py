from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


ContactsOperation = Literal[
    "list_contacts",
    "count_contacts",
    "search_contact",
    "get_contact_phone",
    "get_contact_email",
    "get_contact_details",
    "unknown",
]
RequestedAttribute = Literal["phone", "email", "details", "all", "none"]
Confidence = Literal["low", "medium", "high"]


class ContactsReadTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None

    @field_validator("name", "email", "phone", mode="before")
    @classmethod
    def _normalize_optional_str(cls, value):
        if value is None:
            return None
        if not isinstance(value, str):
            raise TypeError("Target fields must be strings.")
        normalized = " ".join(value.strip().split())
        return normalized or None


class ContactsReadExtraction(BaseModel):
    """LLM-produced structured Contacts read intent. No IDs/connection/user fields."""

    model_config = ConfigDict(extra="forbid", strict=True)

    operation: ContactsOperation
    target: ContactsReadTarget = Field(default_factory=ContactsReadTarget)
    requested_attribute: RequestedAttribute = "none"
    confidence: Confidence = "medium"
    ambiguity_reason: Optional[str] = None
    requires_clarification: bool = False

    @field_validator("ambiguity_reason", mode="before")
    @classmethod
    def _normalize_reason(cls, value):
        if value is None:
            return None
        if not isinstance(value, str):
            raise TypeError("ambiguity_reason must be a string.")
        normalized = " ".join(value.strip().split())
        return normalized or None
