
import hashlib
import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from supabase import Client
from app.schemas.email_draft import EmailDraftPreview


class MessageService:
    GMAIL_SAVE_STATE_FILTER = "metadata->gmail_draft_save->>state"
    GMAIL_SAVE_OPERATION_FILTER = (
        "metadata->gmail_draft_save->>operation_id"
    )

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

        save_record = metadata.get("gmail_draft_save") or {}
        if isinstance(save_record, dict) and save_record.get("state") in {
            "saving",
            "uncertain",
        }:
            raise ValueError(
                "This draft has a Gmail save in progress or an uncertain result."
            )

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
            if (
                email_draft != existing_draft
                and isinstance(save_record, dict)
                and save_record.get("state") == "failed"
            ):
                updated_metadata.pop("gmail_draft_save", None)

        updated_result = (
            self.supabase
            .table("pa_conversation_messages")
            .update({"metadata": updated_metadata})
            .eq("id", str(message_id))
            .eq("conversation_id", str(conversation_id))
            .eq("role", "assistant")
            .filter(
                "metadata",
                "eq",
                json.dumps(metadata, separators=(",", ":")),
            )
            .select("*")
            .limit(1)
            .execute()
        )
        if not updated_result.data:
            raise ValueError(
                "The draft changed while it was being updated. Reload and retry."
            )
        return updated_result.data[0]

    def get_email_draft_for_gmail_save(
        self,
        user_id: UUID,
        conversation_id: UUID,
        message_id: UUID,
        connection_id: UUID,
    ) -> tuple[dict[str, Any], EmailDraftPreview] | None:
        if not self.verify_conversation_ownership(
            user_id=user_id,
            conversation_id=conversation_id,
        ):
            return None

        result = (
            self.supabase
            .table("pa_conversation_messages")
            .select("*")
            .eq("id", str(message_id))
            .eq("conversation_id", str(conversation_id))
            .eq("role", "assistant")
            .limit(1)
            .execute()
        )
        if not result.data:
            return None

        message = result.data[0]
        metadata = message.get("metadata") or {}
        if not isinstance(metadata, dict) or "email_draft" not in metadata:
            return None

        try:
            draft = EmailDraftPreview.model_validate(metadata["email_draft"])
        except ValidationError as exc:
            raise ValueError(
                "The saved email draft preview is invalid."
            ) from exc

        if (
            draft.draft_type != "new"
            and metadata.get("email_draft_connection_id")
            != str(connection_id)
        ):
            raise ValueError(
                "This reply or forward preview belongs to a different Gmail account. "
                "Regenerate it using the selected account."
            )
        return message, draft

    @staticmethod
    def email_draft_fingerprint(draft: EmailDraftPreview) -> str:
        canonical = json.dumps(
            draft.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def claim_gmail_draft_save(
        self,
        user_id: UUID,
        conversation_id: UUID,
        message_id: UUID,
        connection_id: UUID,
        operation_id: UUID,
        fingerprint: str,
    ) -> dict[str, Any]:
        current = self.get_email_draft_for_gmail_save(
            user_id,
            conversation_id,
            message_id,
            connection_id,
        )
        if current is None:
            return {"status": "not_found"}

        message, _draft = current
        metadata = message.get("metadata") or {}
        old_record = metadata.get("gmail_draft_save") or {}
        if not isinstance(old_record, dict):
            old_record = {}

        state = old_record.get("state")
        if (
            old_record.get("saved_fingerprint") == fingerprint
            and old_record.get("saved_connection_id")
            == str(connection_id)
            and old_record.get("saved_result")
        ):
            return {
                "status": "saved",
                "message": message,
                "record": old_record,
            }
        if state == "saved" and not (
            isinstance(old_record.get("saved_result"), dict)
            and old_record["saved_result"].get("draft_id")
        ):
            return {
                "status": "uncertain",
                "message": message,
                "record": old_record,
            }
        if state in {"saving", "uncertain"}:
            return {
                "status": state,
                "message": message,
                "record": old_record,
            }
        if state not in {None, "saved", "failed"}:
            return {"status": "unavailable"}

        new_record: dict[str, Any] = {
            "state": "saving",
            "operation_id": str(operation_id),
            "fingerprint": fingerprint,
            "connection_id": str(connection_id),
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        for field in (
            "saved_result",
            "saved_preview",
            "saved_operation_id",
            "saved_fingerprint",
            "saved_connection_id",
        ):
            if field in old_record:
                new_record[field] = old_record[field]
        if isinstance(old_record.get("history"), list):
            new_record["history"] = old_record["history"][-9:]

        updated_metadata = dict(metadata)
        updated_metadata["gmail_draft_save"] = new_record
        query = (
            self.supabase
            .table("pa_conversation_messages")
            .update({"metadata": updated_metadata})
            .eq("id", str(message_id))
            .eq("conversation_id", str(conversation_id))
            .eq("role", "assistant")
            .filter(
                "metadata",
                "eq",
                json.dumps(metadata, separators=(",", ":")),
            )
        )
        if state is None:
            query = query.filter(
                self.GMAIL_SAVE_STATE_FILTER,
                "is",
                "null",
            )
        else:
            query = query.filter(
                self.GMAIL_SAVE_STATE_FILTER,
                "eq",
                state,
            )
            if old_record.get("operation_id"):
                query = query.filter(
                    self.GMAIL_SAVE_OPERATION_FILTER,
                    "eq",
                    str(old_record["operation_id"]),
                )
        claimed = query.select("*").limit(1).execute()
        if claimed.data:
            return {
                "status": "claimed",
                "message": claimed.data[0],
                "record": new_record,
                "draft": _draft,
            }

        return {"status": "conflict"}

    def finish_gmail_draft_save(
        self,
        user_id: UUID,
        conversation_id: UUID,
        message_id: UUID,
        operation_id: UUID,
        state: str,
        result: dict[str, Any] | None = None,
        saved_preview: dict[str, Any] | None = None,
        allow_uncertain: bool = False,
    ) -> dict[str, Any] | None:
        if state not in {"saved", "failed", "uncertain"}:
            raise ValueError("Invalid Gmail draft save state.")
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

        message = current_result.data[0]
        metadata = message.get("metadata") or {}
        save_record = metadata.get("gmail_draft_save") or {}
        allowed_states = (
            {"saving", "uncertain", "saved"}
            if allow_uncertain
            else {"saving"}
        )
        if (
            not isinstance(save_record, dict)
            or save_record.get("state") not in allowed_states
            or save_record.get("operation_id") != str(operation_id)
        ):
            return None

        updated_record = dict(save_record)
        updated_record["state"] = state
        if result is not None:
            history = list(updated_record.get("history") or [])
            if updated_record.get("saved_result"):
                history.append({
                    "operation_id": updated_record.get("saved_operation_id"),
                    "fingerprint": updated_record.get("saved_fingerprint"),
                    "result": updated_record["saved_result"],
                    "preview": updated_record.get("saved_preview"),
                    "connection_id": updated_record.get(
                        "saved_connection_id"
                    ),
                })
            updated_record["history"] = history[-10:]
            updated_record["saved_result"] = result
            updated_record["saved_operation_id"] = str(operation_id)
            updated_record["saved_fingerprint"] = (
                updated_record.get("fingerprint")
            )
            updated_record["saved_connection_id"] = (
                updated_record.get("connection_id")
            )
        if saved_preview is not None:
            updated_record["saved_preview"] = saved_preview

        updated_metadata = dict(metadata)
        updated_metadata["gmail_draft_save"] = updated_record
        updated_result = (
            self.supabase
            .table("pa_conversation_messages")
            .update({"metadata": updated_metadata})
            .eq("id", str(message_id))
            .eq("conversation_id", str(conversation_id))
            .eq("role", "assistant")
            .filter(
                self.GMAIL_SAVE_STATE_FILTER,
                "eq",
                save_record["state"],
            )
            .filter(
                self.GMAIL_SAVE_OPERATION_FILTER,
                "eq",
                str(operation_id),
            )
            .select("*")
            .limit(1)
            .execute()
        )
        return updated_result.data[0] if updated_result.data else None

    def get_gmail_draft_save_record(
        self,
        user_id: UUID,
        conversation_id: UUID,
        message_id: UUID,
        connection_id: UUID,
        operation_id: UUID,
    ) -> dict[str, Any]:
        if not self.verify_conversation_ownership(
            user_id=user_id,
            conversation_id=conversation_id,
        ):
            return {"status": "not_found"}

        result = (
            self.supabase
            .table("pa_conversation_messages")
            .select("*")
            .eq("id", str(message_id))
            .eq("conversation_id", str(conversation_id))
            .eq("role", "assistant")
            .limit(1)
            .execute()
        )
        if not result.data:
            return {"status": "not_found"}

        message = result.data[0]
        metadata = message.get("metadata") or {}
        save_record = (
            metadata.get("gmail_draft_save")
            if isinstance(metadata, dict)
            else None
        )
        if not isinstance(save_record, dict):
            return {"status": "not_found"}

        operation_value = str(operation_id)
        if save_record.get("operation_id") == operation_value:
            if str(save_record.get("connection_id")) != str(connection_id):
                raise ValueError("This save operation belongs to another Gmail account.")
            return {
                "status": save_record.get("state", "not_found"),
                "message": message,
                "record": save_record,
            }

        if (
            save_record.get("saved_operation_id") == operation_value
            and str(save_record.get("saved_connection_id")) == str(connection_id)
        ):
            return {
                "status": "saved",
                "message": message,
                "record": {
                    **save_record,
                    "saved_result": save_record.get("saved_result"),
                    "saved_preview": save_record.get("saved_preview"),
                    "saved_fingerprint": save_record.get("saved_fingerprint"),
                },
            }

        history = save_record.get("history")
        if isinstance(history, list):
            for entry in history:
                if (
                    isinstance(entry, dict)
                    and entry.get("operation_id") == operation_value
                    and str(entry.get("connection_id")) == str(connection_id)
                ):
                    return {
                        "status": "saved",
                        "message": message,
                        "record": {
                            **save_record,
                            "saved_result": entry.get("result"),
                            "saved_preview": entry.get("preview"),
                            "saved_connection_id": entry.get("connection_id"),
                            "saved_operation_id": operation_value,
                            "saved_fingerprint": entry.get("fingerprint"),
                        },
                    }

        return {"status": "not_found"}


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
