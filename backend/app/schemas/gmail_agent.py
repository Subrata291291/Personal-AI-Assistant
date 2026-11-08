from datetime import date
from typing import Annotated, Literal, TypeAlias
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

from app.schemas.email_draft import normalize_email_address


class GmailPlanBase(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
    )


class GeneralPlan(GmailPlanBase):
    intent: Literal["general"]


class DailyPlanningPlan(GmailPlanBase):
    intent: Literal["daily_planning"]


class ClarifyPlan(GmailPlanBase):
    intent: Literal["clarify"]


class UnsupportedPlan(GmailPlanBase):
    intent: Literal["unsupported"]


class IntegrationPlan(GmailPlanBase):
    intent: Literal[
        "google_drive",
        "google_calendar",
        "google_contacts",
        "tasks",
    ]
    calendar_tool: Literal[
        "list_calendars",
        "list_calendar_events",
        "get_calendar_event",
        "get_today_schedule",
        "get_calendar_schedule",
        "check_calendar_availability",
        "create_calendar_event_proposal",
        "update_calendar_event_proposal",
        "delete_calendar_event_proposal",
        "get_calendar_proposal_status",
        "verify_calendar_proposal_event",
        "clarify",
    ] | None = None
    calendar_arguments: dict[str, object] | None = None
    supersedes_proposal_id: str | None = None


class MeetingDiscoveryPlan(GmailPlanBase):
    intent: Literal["discover_meetings"]
    target_date: date
    max_results: int = Field(default=10, ge=1, le=10)


class ApprovalWorkflowPlan(GmailPlanBase):
    intent: Literal["approval_workflow"]
    operation: Literal["approve", "reject", "execute"]


class DraftEmailPlan(GmailPlanBase):
    intent: Literal[
        "draft_email",
        "gmail_compose",
        "gmail_save_draft",
        "gmail_send",
        "gmail_reply",
        "gmail_reply_all",
        "gmail_forward",
    ]
    draft_type: Literal["new", "reply", "reply_all", "forward"] = "new"
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
    to: list[str] = Field(default_factory=list, max_length=20)
    cc: list[str] = Field(default_factory=list, max_length=20)
    bcc: list[str] = Field(default_factory=list, max_length=20)
    subject: str | None = Field(default=None, max_length=500)
    purpose: str | None = Field(default=None, max_length=2000)
    content_requirements: list[str] = Field(default_factory=list, max_length=20)
    tone: str | None = Field(default=None, max_length=100)
    requested_action: Literal["compose", "save_draft", "send"] = "compose"

    @field_validator("to", "cc", "bcc")
    @classmethod
    def validate_recipients(cls, value: list[str]) -> list[str]:
        normalized = [normalize_email_address(address) for address in value]
        if any(address is None for address in normalized):
            raise ValueError("A compose recipient email address is invalid.")
        return [address for address in normalized if address is not None]


class GmailReadPlan(GmailPlanBase):
    intent: Literal["gmail_read"]
    message_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    thread_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        pattern=r"^[A-Za-z0-9_-]+$",
    )

    @model_validator(mode="after")
    def require_one_source(self) -> "GmailReadPlan":
        if bool(self.message_id) == bool(self.thread_id):
            raise ValueError("Provide exactly one Gmail message or thread ID.")
        return self


class GmailUnavailableActionPlan(GmailPlanBase):
    intent: Literal["gmail_archive", "gmail_delete"]
    message_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    query: str | None = Field(default=None, min_length=1, max_length=500)


class SearchEmailsPlan(GmailPlanBase):
    intent: Literal["search_emails", "gmail_search"]
    query: str | None = Field(default=None, min_length=1, max_length=500)
    sender: str | None = Field(default=None, min_length=1, max_length=320)
    subject: str | None = Field(default=None, min_length=1, max_length=200)
    after: date | None = None
    before: date | None = None
    has_attachment: bool | None = None
    max_results: int = Field(default=5, ge=1, le=10)

    @field_validator("query", "sender", "subject")
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
                self.query,
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


class SummarizeImportantPlan(GmailPlanBase):
    intent: Literal["summarize_important"]
    max_results: int = Field(default=10, ge=1, le=10)


class SummarizeUrgentPlan(GmailPlanBase):
    intent: Literal["summarize_urgent"]
    max_results: int = Field(default=10, ge=1, le=10)


class SummarizeAttentionPlan(GmailPlanBase):
    intent: Literal["summarize_attention"]
    max_results: int = Field(default=10, ge=1, le=10)


class SummarizeTodayPlan(GmailPlanBase):
    intent: Literal["summarize_today"]
    max_results: int = Field(default=10, ge=1, le=10)


class SummarizeLatestThreadPlan(GmailPlanBase):
    intent: Literal["summarize_latest_thread"]
    query: str = Field(min_length=1, max_length=500)
    max_results: int = Field(default=5, ge=1, le=10)

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ord(character) < 32 for character in value):
            raise ValueError("Search query must be non-empty text.")
        return value


class GetEmailPlan(GmailPlanBase):
    intent: Literal["get_email"]
    message_id: str = Field(
        min_length=1,
        max_length=255,
        pattern=r"^[A-Za-z0-9_-]+$",
    )


class GetLatestEmailPlan(GmailPlanBase):
    intent: Literal["get_latest_email"]
    sender: str = Field(
        min_length=1,
        max_length=320,
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


class MeetingEventExtraction(GmailPlanBase):
    title: str = Field(min_length=1, max_length=250)
    event_date: date | None = None
    start_time: str | None = Field(default=None, max_length=40)
    end_time: str | None = Field(default=None, max_length=40)
    timezone: str | None = Field(default=None, max_length=100)
    organizer: str | None = Field(default=None, max_length=320)
    participants: list[str] = Field(default_factory=list, max_length=30)
    location: str | None = Field(default=None, max_length=500)
    meeting_url: str | None = Field(default=None, max_length=2048)
    status: Literal[
        "confirmed",
        "proposed",
        "invitation_awaiting_confirmation",
        "mentioned_without_confirmed_time",
    ]
    source_thread_id: str = Field(
        min_length=1,
        max_length=255,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    date_evidence: str | None = Field(default=None, max_length=500)
    start_time_evidence: str | None = Field(default=None, max_length=500)
    end_time_evidence: str | None = Field(default=None, max_length=500)
    status_evidence: str | None = Field(default=None, max_length=1000)


class MeetingExtractionResponse(GmailPlanBase):
    events: list[MeetingEventExtraction] = Field(default_factory=list, max_length=10)


AssistantIntent: TypeAlias = Annotated[
    GeneralPlan
    | DailyPlanningPlan
    | ClarifyPlan
    | UnsupportedPlan
    | IntegrationPlan
    | MeetingDiscoveryPlan
    | ApprovalWorkflowPlan
    | DraftEmailPlan
    | GmailReadPlan
    | GmailUnavailableActionPlan
    | SearchEmailsPlan
    | SummarizeImportantPlan
    | SummarizeUrgentPlan
    | SummarizeAttentionPlan
    | SummarizeTodayPlan
    | SummarizeLatestThreadPlan
    | GetEmailPlan
    | GetLatestEmailPlan
    | GetThreadPlan
    | SummarizeUnreadPlan
    | SummarizeThreadPlan,
    Field(discriminator="intent"),
]

GmailPlan: TypeAlias = AssistantIntent
assistant_intent_adapter = TypeAdapter(AssistantIntent)


def parse_assistant_intent(raw_plan: str) -> AssistantIntent:
    """Parse only strict JSON decisions matching supported intent schemas."""
    return assistant_intent_adapter.validate_json(raw_plan)


def parse_gmail_plan(raw_plan: str) -> AssistantIntent:
    """Backward-compatible parser name for the unified assistant intents."""
    return parse_assistant_intent(raw_plan)
