from typing import Annotated, Any, NotRequired, TypedDict
from operator import add


class ChatMessage(TypedDict):
    role: str
    content: str


class ChatContext(TypedDict):
    user_id: str
    gmail_connection_id: str | None
    user_timezone: NotRequired[str]
    request_time: NotRequired[str]
    calendar_event_connection_id: NotRequired[str | None]
    calendar_write_connection_id: NotRequired[str | None]
    calendar_write_connection_email: NotRequired[str | None]
    calendar_freebusy_connection_id: NotRequired[str | None]
    google_connection_available: NotRequired[bool]
    calendar_default_connection_selected: NotRequired[bool]
    calendar_default_connection_invalid: NotRequired[bool]
    calendar_selected_account_email: NotRequired[str | None]
    calendar_event_selection_required: NotRequired[bool]
    calendar_freebusy_selection_required: NotRequired[bool]
    calendar_selected_event_scope_missing: NotRequired[bool]
    calendar_write_selection_required: NotRequired[bool]
    calendar_selected_write_scope_missing: NotRequired[bool]
    calendar_selected_freebusy_scope_missing: NotRequired[bool]


class ChatState(TypedDict):

    user_id: str

    conversation_id: str

    user_message: str

    user_timezone: NotRequired[str]

    request_time: NotRequired[str]

    assistant_capabilities: NotRequired[dict[str, Any]]

    pending_calendar_create_proposal: NotRequired[dict[str, Any]]

    calendar_proposal_supersedes_id: NotRequired[str]

    assistant_route: NotRequired[str]

    gmail_plan: NotRequired[dict[str, Any]]
    calendar_create_event_proposal: NotRequired[dict[str, Any]]

    assistant_response: str

    provider: str

    model: str

    messages: Annotated[list[ChatMessage], add]

    response_metadata: NotRequired[dict[str, Any]]

    # Persistent memory fields
    retrieved_memories: NotRequired[list[dict[str, Any]]]

    memory_context: NotRequired[str]

    available_gmail_sources: NotRequired[dict[str, Any]]

    available_email_draft: NotRequired[bool]

    gmail_sources: NotRequired[dict[str, Any]]

    email_draft: NotRequired[dict[str, Any]]