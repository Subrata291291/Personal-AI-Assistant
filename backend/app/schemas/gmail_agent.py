from datetime import date
from typing import Annotated, Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)


class GmailPlanBase(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
    )


class GeneralPlan(GmailPlanBase):
    intent: Literal["general"]


class ClarifyPlan(GmailPlanBase):
    intent: Literal["clarify"]


class UnsupportedPlan(GmailPlanBase):
    intent: Literal["unsupported"]


class DraftEmailPlan(GmailPlanBase):
    intent: Literal["draft_email"]
    draft_type: Literal["new", "reply", "reply_all", "forward"]
    source_message_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    source_thread_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        pattern=r"^[A-Za-z0-9_-]+$",
    )


class SearchEmailsPlan(GmailPlanBase):
    intent: Literal["search_emails"]
    sender: str | None = Field(default=None, min_length=1, max_length=320)
    subject: str | None = Field(default=None, min_length=1, max_length=200)
    after: date | None = None
    before: date | None = None
    has_attachment: bool | None = None
    max_results: int = Field(default=5, ge=1, le=10)

    @field_validator("sender", "subject")
    @classmethod
    def normalize_search_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("Search criteria cannot be empty.")
        if any(ord(character) < 32 for character in value):
            raise ValueError("Search criteria cannot contain control characters.")
        return value

    @model_validator(mode="after")
    def validate_search_criteria(self) -> "SearchEmailsPlan":
        if not any(
            value is not None
            for value in (
                self.sender,
                self.subject,
                self.after,
                self.before,
                self.has_attachment,
            )
        ):
            raise ValueError("At least one search criterion is required.")

        if self.after and self.before and self.after >= self.before:
            raise ValueError("'after' must be earlier than 'before'.")

        return self


class GetEmailPlan(GmailPlanBase):
    intent: Literal["get_email"]
    message_id: str = Field(
        min_length=1,
        max_length=255,
        pattern=r"^[A-Za-z0-9_-]+$",
    )


class GetThreadPlan(GmailPlanBase):
    intent: Literal["get_thread"]
    thread_id: str = Field(
        min_length=1,
        max_length=255,
        pattern=r"^[A-Za-z0-9_-]+$",
    )


class SummarizeUnreadPlan(GmailPlanBase):
    intent: Literal["summarize_unread"]
    max_results: int = Field(default=5, ge=1, le=5)


class SummarizeThreadPlan(GmailPlanBase):
    intent: Literal["summarize_thread"]
    thread_id: str = Field(
        min_length=1,
        max_length=255,
        pattern=r"^[A-Za-z0-9_-]+$",
    )


GmailPlan: TypeAlias = Annotated[
    GeneralPlan
    | ClarifyPlan
    | UnsupportedPlan
    | DraftEmailPlan
    | SearchEmailsPlan
    | GetEmailPlan
    | GetThreadPlan
    | SummarizeUnreadPlan
    | SummarizeThreadPlan,
    Field(discriminator="intent"),
]

gmail_plan_adapter = TypeAdapter(GmailPlan)


def parse_gmail_plan(raw_plan: str) -> GmailPlan:
    """Parse only strict JSON plans matching the supported intent schemas."""
    return gmail_plan_adapter.validate_json(raw_plan)
