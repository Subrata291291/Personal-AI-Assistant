from typing import Annotated, Any, NotRequired, TypedDict
from operator import add


class ChatMessage(TypedDict):
    role: str
    content: str


class ChatContext(TypedDict):
    user_id: str
    gmail_connection_id: str | None
    user_timezone: NotRequired[str]


class ChatState(TypedDict):

    user_id: str

    conversation_id: str

    user_message: str

    user_timezone: NotRequired[str]

    assistant_route: NotRequired[str]

    gmail_plan: NotRequired[dict[str, Any]]

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