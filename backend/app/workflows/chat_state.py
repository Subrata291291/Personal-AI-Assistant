from typing import Annotated, Any, NotRequired, TypedDict
from operator import add


class ChatMessage(TypedDict):
    role: str
    content: str


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