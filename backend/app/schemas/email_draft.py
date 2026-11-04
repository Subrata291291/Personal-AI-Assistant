import re
from email.utils import parseaddr
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_EMAIL_PATTERN = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)
_MESSAGE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,255}$")


def normalize_email_address(address: str) -> str | None:
    address = address.strip()
    if "<" in address or ">" in address:
        formatted = re.fullmatch(r'\s*(.*?)<\s*([^<>]+)\s*>\s*', address)
        if not formatted:
            return None
        _, parsed = parseaddr(address)
        address = formatted.group(2).strip()
        if parsed != address:
            return None
    local_part, separator, domain = address.rpartition("@")
    if (
        len(address) > 320
        or not separator
        or len(local_part) > 64
        or len(domain) > 253
        or local_part.startswith(".")
        or local_part.endswith(".")
        or not _EMAIL_PATTERN.fullmatch(address)
        or ".." in local_part
    ):
        return None
    return address


class EmailRecipient(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    email: str = Field(max_length=320)
    name: str | None = Field(default=None, max_length=200)

    @field_validator("email", mode="before")
    @classmethod
    def validate_email(cls, value: object) -> object:
        if not isinstance(value, str):
            raise ValueError("A recipient email address is invalid.")
        address = normalize_email_address(value)
        if address is None:
            raise ValueError("A recipient email address is invalid.")
        return address

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str | None) -> str | None:
        if value is not None and (
            any(ord(character) < 32 or ord(character) == 127 for character in value)
            or "<" in value
            or ">" in value
        ):
            raise ValueError("A recipient display name is invalid.")
        return value.strip() or None if value is not None else None


class EmailDraftPreview(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    draft_type: Literal["new", "reply", "reply_all", "forward"]
    to: list[EmailRecipient] = Field(default_factory=list, max_length=20)
    cc: list[EmailRecipient] = Field(default_factory=list, max_length=20)
    bcc: list[EmailRecipient] = Field(default_factory=list, max_length=20)
    subject: str = Field(max_length=500)
    body: str = Field(min_length=1, max_length=20000)
    source_message_id: str | None = Field(default=None, max_length=255)
    source_thread_id: str | None = Field(default=None, max_length=255)

    @field_validator("to", "cc", "bcc", mode="before")
    @classmethod
    def normalize_addresses(cls, value: object) -> object:
        if not isinstance(value, list):
            return value

        normalized: list[dict[str, str | None]] = []
        for recipient in value:
            if isinstance(recipient, str):
                name, address = parseaddr(recipient.strip())
                candidate = {"email": address, "name": name or None}
            elif isinstance(recipient, dict):
                address = recipient.get("email") or recipient.get("address")
                name = (
                    recipient.get("name")
                    or recipient.get("display_name")
                    or recipient.get("displayName")
                )
                if isinstance(address, str) and not name:
                    name, address = parseaddr(address.strip())
                candidate = {"email": address, "name": name}
            else:
                raise ValueError("A recipient must include an email address.")
            normalized.append(candidate)
        return normalized

    @field_validator("subject")
    @classmethod
    def validate_subject(cls, value: str) -> str:
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("The subject cannot contain control characters.")
        return value.strip()

    @field_validator("body")
    @classmethod
    def validate_body(cls, value: str) -> str:
        if any(
            (ord(character) < 32 and character not in "\r\n\t")
            or ord(character) == 127
            for character in value
        ):
            raise ValueError("The body contains unsupported control characters.")
        value = value.strip()
        if not value:
            raise ValueError("The body cannot be empty.")
        return value

    @field_validator("source_message_id", "source_thread_id")
    @classmethod
    def validate_source_ids(cls, value: str | None) -> str | None:
        if value is not None and not _MESSAGE_ID_PATTERN.fullmatch(value):
            raise ValueError("A source message or thread ID is invalid.")
        return value

    @model_validator(mode="after")
    def validate_draft(self) -> "EmailDraftPreview":
        addresses = [
            recipient.email.casefold()
            for group in (self.to, self.cc, self.bcc)
            for recipient in group
        ]
        if len(addresses) != len(set(addresses)):
            raise ValueError("A recipient cannot appear more than once.")

        if self.draft_type == "new" and (
            self.source_message_id or self.source_thread_id
        ):
            raise ValueError("A new draft cannot reference a source email.")
        return self


class EmailDraftUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    email_draft: EmailDraftPreview | None


class GmailDraftSaveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    connection_id: UUID
    operation_id: UUID


class GmailDraftSaveResponse(BaseModel):
    state: Literal["saved"]
    connection_id: UUID
    operation_id: UUID
    draft_id: str
    message_id: str | None = None
    thread_id: str | None = None
    fingerprint: str
    saved_preview: EmailDraftPreview
    repeated: bool = False
    persistence_warning: str | None = None


class GmailDraftSaveStatus(BaseModel):
    state: Literal["saved", "saving", "uncertain", "failed", "not_found"]
    connection_id: UUID
    operation_id: UUID
    draft_id: str | None = None
    message_id: str | None = None
    thread_id: str | None = None
    fingerprint: str | None = None
    saved_preview: EmailDraftPreview | None = None
    detail: str
