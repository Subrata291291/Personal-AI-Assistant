
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from supabase import Client


class ConversationService:

    def __init__(self, supabase: Client):
        self.supabase = supabase

    def create_conversation(
        self,
        user_id: UUID,
        title: str,
    ) -> dict[str, Any]:

        result = (
            self.supabase
            .table("pa_conversations")
            .insert({
                "user_id": str(user_id),
                "title": title.strip(),
            })
            .execute()
        )

        if not result.data:
            raise RuntimeError("Conversation creation failed.")

        return result.data[0]

    def list_conversations(
        self,
        user_id: UUID,
    ) -> list[dict[str, Any]]:

        result = (
            self.supabase
            .table("pa_conversations")
            .select("*")
            .eq("user_id", str(user_id))
            .order("created_at", desc=True)
            .execute()
        )

        return result.data

    def get_conversation(
        self,
        user_id: UUID,
        conversation_id: UUID,
    ) -> dict[str, Any] | None:

        result = (
            self.supabase
            .table("pa_conversations")
            .select("*")
            .eq("id", str(conversation_id))
            .eq("user_id", str(user_id))
            .limit(1)
            .execute()
        )

        if not result.data:
            return None

        return result.data[0]

    def update_conversation(
        self,
        user_id: UUID,
        conversation_id: UUID,
        title: str,
    ) -> dict[str, Any] | None:

        result = (
            self.supabase
            .table("pa_conversations")
            .update({
                "title": title.strip(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            .eq("id", str(conversation_id))
            .eq("user_id", str(user_id))
            .select("*")
            .execute()
        )

        if not result.data:
            return None

        return result.data[0]

    def archive_conversation(
        self,
        user_id: UUID,
        conversation_id: UUID,
    ) -> dict[str, Any] | None:

        result = (
            self.supabase
            .table("pa_conversations")
            .update({
                "is_archived": True,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            .eq("id", str(conversation_id))
            .eq("user_id", str(user_id))
            .select("*")
            .execute()
        )

        if not result.data:
            return None

        return result.data[0]

    def delete_conversation(
        self,
        user_id: UUID,
        conversation_id: UUID,
    ) -> bool:

        result = (
            self.supabase
            .table("pa_conversations")
            .delete()
            .eq("id", str(conversation_id))
            .eq("user_id", str(user_id))
            .select("id")
            .execute()
        )

        return bool(result.data)
