from typing import Annotated, Any, NotRequired, TypedDict
from operator import add


class ChatMessage(TypedDict):
    role: str
    content: str
    metadata: NotRequired[dict[str, Any]]


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
    contacts_connection_id: NotRequired[str | None]
    contacts_write_connection_id: NotRequired[str | None]
    contacts_enabled: NotRequired[bool]
    contacts_selection_required: NotRequired[bool]
    contacts_selected_account_email: NotRequired[str | None]
    contacts_candidate_emails: NotRequired[list[str]]
    contacts_default_connection_invalid: NotRequired[bool]
    contacts_default_connection_selected: NotRequired[bool]
    contacts_read_scope_missing: NotRequired[bool]
    contacts_write_scope_missing: NotRequired[bool]
    contacts_google_connection_available: NotRequired[bool]
    drive_connection_id: NotRequired[str | None]
    drive_enabled: NotRequired[bool]
    drive_selection_required: NotRequired[bool]
    drive_selected_account_email: NotRequired[str | None]
    drive_candidate_emails: NotRequired[list[str]]
    drive_read_scope_missing: NotRequired[bool]
    drive_default_connection_invalid: NotRequired[bool]
    drive_default_connection_selected: NotRequired[bool]


class ChatState(TypedDict):

    user_id: str

    conversation_id: str

    user_message: str

    user_timezone: NotRequired[str]

    request_time: NotRequired[str]

    memory_update_result: NotRequired[dict[str, Any]]
    memory_decision_result: NotRequired[dict[str, Any]]
    memory_decision_executed: NotRequired[bool]
    memory_enabled: NotRequired[bool]
    memory_execution_trace: NotRequired[dict[str, Any]]

    assistant_capabilities: NotRequired[dict[str, Any]]

    pending_calendar_action: NotRequired[dict[str, Any]]
    pending_calendar_availability: NotRequired[dict[str, Any]]
    calendar_action_request: NotRequired[str]

    pending_calendar_create_proposal: NotRequired[dict[str, Any]]
    calendar_create_draft: NotRequired[dict[str, Any] | None]

    calendar_proposal_supersedes_id: NotRequired[str]

    assistant_route: NotRequired[str]

    gmail_plan: NotRequired[dict[str, Any]]
    gmail_connection_id: NotRequired[str | None]
    calendar_executed_arguments: NotRequired[dict[str, Any]]
    calendar_action_proposal: NotRequired[dict[str, Any]]
    calendar_create_event_proposal: NotRequired[dict[str, Any]]
    contacts_action_proposal: NotRequired[dict[str, Any]]

    assistant_response: str

    provider: str

    model: str

    messages: Annotated[list[ChatMessage], add]

    response_metadata: NotRequired[dict[str, Any]]
    contacts_provenance: NotRequired[dict[str, Any]]
    calendar_approval_result: NotRequired[dict[str, Any]]
    calendar_action_details: NotRequired[dict[str, Any]]
    email_draft_connection_id: NotRequired[str]

    calendar_events: NotRequired[list[dict[str, Any]]]

    # Persistent memory fields
    retrieved_memories: NotRequired[list[dict[str, Any]]]

    memory_context: NotRequired[str]

    available_gmail_sources: NotRequired[dict[str, Any]]

    available_email_draft: NotRequired[bool]

    gmail_sources: NotRequired[dict[str, Any]]

    email_draft: NotRequired[dict[str, Any]]

    pending_contacts_clarification: NotRequired[dict[str, Any] | None]

    pending_calendar_delete_selection: NotRequired[dict[str, Any] | None]

    pending_calendar_read: NotRequired[dict[str, Any] | None]

    pending_gmail_clarification: NotRequired[dict[str, Any] | None]

    calendar_passthrough_response: NotRequired[bool]