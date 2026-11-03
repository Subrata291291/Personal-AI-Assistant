import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_EMAIL_PATTERN = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)
_MESSAGE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,255}$")


def normalize_email_address(address: str) -> str | None:
    address = address.strip()
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


class EmailDraftPreview(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    draft_type: Literal["new", "reply", "reply_all", "forward"]
    to: list[str] = Field(default_factory=list, max_length=20)
    cc: list[str] = Field(default_factory=list, max_length=20)
    bcc: list[str] = Field(default_factory=list, max_length=20)
    subject: str = Field(max_length=500)
    body: str = Field(min_length=1, max_length=20000)
    source_message_id: str | None = Field(default=None, max_length=255)
    source_thread_id: str | None = Field(default=None, max_length=255)

    @field_validator("to", "cc", "bcc", mode="before")
    @classmethod
    def normalize_addresses(cls, value: object) -> object:
        if not isinstance(value, list):
            return value

        normalized: list[str] = []
        for address in value:
            if not isinstance(address, str):
                raise ValueError("Recipients must be email address strings.")
            normalized_address = normalize_email_address(address)
            if normalized_address is None:
                raise ValueError("A recipient email address is invalid.")
            normalized.append(normalized_address)
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
            address.casefold()
            for group in (self.to, self.cc, self.bcc)
            for address in group
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
