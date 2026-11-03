
from typing import Any
from uuid import UUID

from supabase import Client


class MessageService:

    def __init__(self, supabase: Client):
        self.supabase = supabase

    def verify_conversation_ownership(
        self,
        user_id: UUID,
        conversation_id: UUID,
    ) -> bool:
        """
        Verify that the authenticated user owns the conversation.
        """

        result = (
            self.supabase
            .table("pa_conversations")
            .select("id")
            .eq("id", str(conversation_id))
            .eq("user_id", str(user_id))
            .limit(1)
            .execute()
        )

        return bool(result.data)

    def create_message(
        self,
        conversation_id: UUID,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Create a message inside an authorized conversation.

        This method is intended for trusted backend workflows.
        """

        allowed_roles = {"user", "assistant", "system", "tool"}

        if role not in allowed_roles:
            raise ValueError("Invalid message role.")

        if not content or not content.strip():
            raise ValueError("Message content cannot be empty.")

        payload = {
            "conversation_id": str(conversation_id),
            "role": role,
            "content": content.strip(),
            "metadata": metadata,
        }

        result = (
            self.supabase
            .table("pa_conversation_messages")
            .insert(payload)
            .execute()
        )

        if not result.data:
            raise RuntimeError("Message creation failed.")

        return result.data[0]

    
    def create_request_message(
        self,
        conversation_id: UUID,
        chat_request_id: UUID,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Create one message associated with a chat request.

        The database unique index guarantees at most one
        message per chat_request_id and role.
        """

        allowed_roles = {"user", "assistant"}

        if role not in allowed_roles:
            raise ValueError("Invalid request message role.")

        if not content or not content.strip():
            raise ValueError("Message content cannot be empty.")

        payload = {
            "conversation_id": str(conversation_id),
            "chat_request_id": str(chat_request_id),
            "role": role,
            "content": content.strip(),
            "metadata": metadata,
        }

        result = (
            self.supabase
            .table("pa_conversation_messages")
            .insert(payload)
            .execute()
        )

        if not result.data:
            raise RuntimeError("Request message creation failed.")

        return result.data[0]

    def get_request_message(
        self,
        user_id: UUID,
        conversation_id: UUID,
        chat_request_id: UUID,
        role: str,
    ) -> dict[str, Any] | None:
        """
        Retrieve a message associated with a specific request.
        """

        if role not in {"user", "assistant"}:
            raise ValueError("Invalid request message role.")

        if not self.verify_conversation_ownership(
            user_id=user_id,
            conversation_id=conversation_id,
        ):
            raise LookupError("Conversation not found.")

        result = (
            self.supabase
            .table("pa_conversation_messages")
            .select("*")
            .eq("conversation_id", str(conversation_id))
            .eq("chat_request_id", str(chat_request_id))
            .eq("role", role)
            .limit(1)
            .execute()
        )

        return result.data[0] if result.data else None


    def create_user_message(
        self,
        user_id: UUID,
        conversation_id: UUID,
        content: str,
    ) -> dict[str, Any]:
        """
        Create a message from an authenticated user.
        """

        if not self.verify_conversation_ownership(
            user_id,
            conversation_id,
        ):
            raise LookupError("Conversation not found.")

        return self.create_message(
            conversation_id=conversation_id,
            role="user",
            content=content,
        )

    def get_conversation_messages(
        self,
        user_id: UUID,
        conversation_id: UUID,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """
        Retrieve messages belonging to the user's conversation.
        """

        if limit < 1 or limit > 500:
            raise ValueError("Limit must be between 1 and 500.")

        if not self.verify_conversation_ownership(
            user_id,
            conversation_id,
        ):
            raise LookupError("Conversation not found.")

        result = (
            self.supabase
            .table("pa_conversation_messages")
            .select("*")
            .eq("conversation_id", str(conversation_id))
            .order("created_at", desc=False)
            .limit(limit)
            .execute()
        )

        return result.data

    
    def get_message(
        self,
        user_id: UUID,
        conversation_id: UUID,
        message_id: UUID,
    ) -> dict[str, Any] | None:
        """
        Retrieve one message from the authenticated user's
        conversation.
        """

        if not self.verify_conversation_ownership(
            user_id=user_id,
            conversation_id=conversation_id,
        ):
            raise LookupError("Conversation not found.")

        result = (
            self.supabase
            .table("pa_conversation_messages")
            .select("*")
            .eq("id", str(message_id))
            .eq("conversation_id", str(conversation_id))
            .limit(1)
            .execute()
        )

        return result.data[0] if result.data else None


    def update_email_draft(
        self,
        user_id: UUID,
        conversation_id: UUID,
        message_id: UUID,
        email_draft: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Update or discard a draft preview on the user's assistant message."""
        if not self.verify_conversation_ownership(
            user_id=user_id,
            conversation_id=conversation_id,
        ):
            return None

        current_result = (
            self.supabase
            .table("pa_conversation_messages")
            .select("*")
            .eq("id", str(message_id))
            .eq("conversation_id", str(conversation_id))
            .eq("role", "assistant")
            .limit(1)
            .execute()
        )
        if not current_result.data:
            return None

        current_message = current_result.data[0]
        metadata = current_message.get("metadata") or {}
        if not isinstance(metadata, dict) or "email_draft" not in metadata:
            return None

        updated_metadata = dict(metadata)
        if email_draft is None:
            updated_metadata.pop("email_draft", None)
        else:
            existing_draft = metadata.get("email_draft")
            if not isinstance(existing_draft, dict) or any(
                email_draft.get(field) != existing_draft.get(field)
                for field in (
                    "draft_type",
                    "source_message_id",
                    "source_thread_id",
                )
            ):
                raise ValueError(
                    "Draft type and source email cannot be changed."
                )
            updated_metadata["email_draft"] = email_draft

        updated_result = (
            self.supabase
            .table("pa_conversation_messages")
            .update({"metadata": updated_metadata})
            .eq("id", str(message_id))
            .eq("conversation_id", str(conversation_id))
            .eq("role", "assistant")
            .select("*")
            .limit(1)
            .execute()
        )
        return updated_result.data[0] if updated_result.data else None


    def delete_message(
        self,
        user_id: UUID,
        message_id: UUID,
        conversation_id: UUID,
    ) -> bool:
        """
        Delete a message only from the user's own conversation.
        """

        if not self.verify_conversation_ownership(
            user_id,
            conversation_id,
        ):
            return False

        result = (
            self.supabase
            .table("pa_conversation_messages")
            .delete()
            .eq("id", str(message_id))
            .eq("conversation_id", str(conversation_id))
            .select("id")
            .execute()
        )

        return bool(result.data)
