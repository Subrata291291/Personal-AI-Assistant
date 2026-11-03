from typing import Annotated, Any, NotRequired, TypedDict
from operator import add


class ChatMessage(TypedDict):
    role: str
    content: str


class ChatContext(TypedDict):
    user_id: str
    gmail_connection_id: str | None


class ChatState(TypedDict):

    user_id: str

    conversation_id: str

    user_message: str

    assistant_response: str

    provider: str

    model: str

    messages: Annotated[list[ChatMessage], add]

    # Persistent memory fields
    retrieved_memories: NotRequired[list[dict[str, Any]]]

    memory_context: NotRequired[str]

    gmail_plan: NotRequired[dict[str, Any]]

    available_gmail_sources: NotRequired[dict[str, Any]]

    gmail_sources: NotRequired[dict[str, Any]]

    email_draft: NotRequired[dict[str, Any]]