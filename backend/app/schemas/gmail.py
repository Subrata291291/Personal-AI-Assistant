from pydantic import BaseModel, ConfigDict, Field, EmailStr
from uuid import UUID

class GmailMessageSummary(BaseModel):
    id: str
    thread_id: str | None = None
    snippet: str = ""
    sender: str | None = None
    recipient: str | None = None
    subject: str | None = None
    internal_date: str | None = None
    date: str | None = None
    labels: list[str] = []


class GmailMessageListResponse(BaseModel):
    messages: list[GmailMessageSummary]
    next_page_token: str | None = None
    result_size_estimate: int = 0


class GmailMessageDetail(BaseModel):
    id: str
    thread_id: str | None = None
    snippet: str = ""
    internal_date: str | None = None
    headers: dict[str, str] = {}
    body: dict[str, str] = {}
    labels: list[str] = []
    attachments: list[dict] = []


class GmailMessageDetailResponse(BaseModel):
    message: GmailMessageDetail

class GmailSendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection_id: str

    to: list[EmailStr] = Field(min_length=1, max_length=100)
    cc: list[EmailStr] = Field(default_factory=list, max_length=100)
    bcc: list[EmailStr] = Field(default_factory=list, max_length=100)

    subject: str = Field(max_length=998)

    body: str = Field(max_length=1_000_000)

    html_body: str | None = Field(
        default=None,
        max_length=1_000_000,
    )

    reply_to: EmailStr | None = None


class GmailReplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection_id: str

    body: str = Field(max_length=1_000_000)

    html_body: str | None = Field(
        default=None,
        max_length=1_000_000,
    )


class GmailForwardRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection_id: str

    to: list[EmailStr] = Field(min_length=1, max_length=100)
    cc: list[EmailStr] = Field(default_factory=list, max_length=100)
    bcc: list[EmailStr] = Field(default_factory=list, max_length=100)

    body: str = Field(default="", max_length=1_000_000)

    html_body: str | None = Field(
        default=None,
        max_length=1_000_000,
    )


class GmailTrashRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection_id: UUID


class GmailMessageActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection_id: UUID