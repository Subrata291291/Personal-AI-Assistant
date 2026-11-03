import json
import re
from email.utils import getaddresses
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.integrations.google.api_client import GoogleAPIClient
from app.integrations.google.email_parser import GmailEmailParser
from app.repositories.oauth_credential_repository import (
    OAuthCredentialRepository,
)
from app.integrations.google.message_builder import (
    GmailMessageBuilder,
)
from app.repositories.user_connection_repository import (
    UserConnectionRepository,
)
from app.integrations.google.attachment_handler import (
    GmailAttachmentHandler,
)

class GmailService:
    """
    Gmail operations for an authenticated user.

    Google API calls use a separate database session so that
    token refresh commits cannot affect unrelated transactions.
    """

    GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
    REQUIRED_READ_SCOPE = (
        "https://www.googleapis.com/auth/gmail.readonly"
    )

    REQUIRED_SEND_SCOPE = (
        "https://www.googleapis.com/auth/gmail.send"
    )

    REQUIRED_MODIFY_SCOPE = (
        "https://www.googleapis.com/auth/gmail.modify"
    )
    REQUIRED_PERMANENT_DELETE_SCOPE = "https://mail.google.com/"

    MAX_LIST_RESULTS = 50
    MAX_THREAD_MESSAGES = 10
    MAX_THREAD_RESPONSE_BYTES = 2_000_000
    MAX_THREAD_BODY_CHARS = 3_000

    def __init__(self, db: Session):
        self.db = db
        self.connection_repository = UserConnectionRepository(db)
        self.credential_repository = OAuthCredentialRepository(db)
        self.api_client = GoogleAPIClient()

    def _validate_connection(
        self,
        user_id: UUID,
        connection_id: UUID,
        required_scope: str | None = None,
    ) -> Any:

        connection = self.connection_repository.get_active_google_connection(
            user_id=user_id,
            connection_id=connection_id,
        )

        if connection is None:
            raise ValueError(
                "Connected Google account was not found."
            )

        credential = self.credential_repository.get_by_connection(
            user_id=user_id,
            connection_id=connection_id,
        )

        if credential is None:
            raise ValueError(
                "Google OAuth credentials were not found."
            )

        granted_scopes = set(credential.scopes or [])

        scope_to_check = (
            required_scope or self.REQUIRED_READ_SCOPE
        )

        if scope_to_check not in granted_scopes:
            if scope_to_check == self.REQUIRED_PERMANENT_DELETE_SCOPE:
                raise PermissionError(
                    "Permanent Gmail deletion requires additional Google "
                    "permission. Reconnect this account and grant access "
                    "to permanently delete Gmail messages."
                )

            raise PermissionError(
                f"Required Google permission has not been granted: "
                f"{scope_to_check}"
            )

        return connection

    async def _google_get(
        self,
        user_id: UUID,
        connection_id: UUID,
        endpoint: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:

        # A dedicated session isolates OAuth token commits.
        api_db = SessionLocal()

        try:
            return await self.api_client.request(
                db=api_db,
                user_id=str(user_id),
                connection_id=str(connection_id),
                method="GET",
                url=f"{self.GMAIL_API_BASE}/{endpoint}",
                params=params,
            )
        finally:
            api_db.close()


    async def _google_post(
        self,
        user_id: UUID,
        connection_id: UUID,
        endpoint: str,
        json_data: dict[str, Any] | None,
    ) -> dict[str, Any]:

        api_db = SessionLocal()

        try:
            return await self.api_client.request(
                db=api_db,
                user_id=str(user_id),
                connection_id=str(connection_id),
                method="POST",
                url=f"{self.GMAIL_API_BASE}/{endpoint}",
                json_data=json_data,
            )
        finally:
            api_db.close()

    async def _google_delete(
        self,
        user_id: UUID,
        connection_id: UUID,
        endpoint: str,
    ) -> dict[str, Any]:
        api_db = SessionLocal()

        try:
            return await self.api_client.request(
                db=api_db,
                user_id=str(user_id),
                connection_id=str(connection_id),
                method="DELETE",
                url=f"{self.GMAIL_API_BASE}/{endpoint}",
            )
        finally:
            api_db.close()
    
    async def list_messages(
        self,
        user_id: UUID,
        connection_id: UUID,
        max_results: int = 20,
        page_token: str | None = None,
        query: str | None = None,
        folder: str = "inbox",
    ) -> dict[str, Any]:

        if not 1 <= max_results <= self.MAX_LIST_RESULTS:
            raise ValueError(
                f"max_results must be between 1 and "
                f"{self.MAX_LIST_RESULTS}."
            )

        self._validate_connection(user_id, connection_id)

        folder_queries = {
            "inbox": "in:inbox",
            "starred": "is:starred",
            "important": "is:important",
            "sent": "in:sent",
            "drafts": "in:drafts",
            "all": "in:anywhere -in:spam -in:trash",
            "spam": "in:spam",
            "trash": "in:trash",
        }

        folder = folder.lower().strip()

        if folder not in folder_queries:
            raise ValueError("Unsupported Gmail folder.")

        folder_query = folder_queries[folder]

        if query and query.strip():
            gmail_query = f"{folder_query} {query.strip()}"
        else:
            gmail_query = folder_query

        params: dict[str, Any] = {
            "maxResults": max_results,
            "q": gmail_query,
        }

        if page_token:
            params["pageToken"] = page_token

        result = await self._google_get(
            user_id=user_id,
            connection_id=connection_id,
            endpoint="messages",
            params=params,
        )

        messages = result.get("messages", [])
        summaries = []

        # Gmail messages.list returns IDs, not full email metadata.
        for item in messages:
            message_id = item.get("id")

            if not message_id:
                continue

            metadata = await self._google_get(
                user_id=user_id,
                connection_id=connection_id,
                endpoint=f"messages/{message_id}",
                params={
                    "format": "metadata",
                    "metadataHeaders": [
                        "From",
                        "To",
                        "Subject",
                        "Date",
                    ],
                },
            )

            headers = GmailEmailParser.extract_headers(
                metadata.get("payload", {})
            )

            summaries.append({
                "id": metadata.get("id"),
                "thread_id": metadata.get("threadId"),
                "snippet": metadata.get("snippet", ""),
                "sender": headers.get("from"),
                "recipient": headers.get("to"),
                "subject": headers.get("subject"),
                "date": headers.get("date"),
                "labels": metadata.get("labelIds", []),
            })

        return {
            "messages": summaries,
            "next_page_token": result.get("nextPageToken"),
            "result_size_estimate": result.get(
                "resultSizeEstimate", 0
            ),
        }


    async def get_message(
        self,
        user_id: UUID,
        connection_id: UUID,
        message_id: str,
    ) -> dict[str, Any]:

        if not isinstance(message_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]+",
            message_id,
        ):
            raise ValueError("Invalid Gmail message ID.")

        self._validate_connection(user_id, connection_id)

        result = await self._google_get(
            user_id=user_id,
            connection_id=connection_id,
            endpoint=f"messages/{message_id}",
            params={
                "format": "full",
            },
        )

        return GmailEmailParser.parse_message(result)

    async def get_thread(
        self,
        user_id: UUID,
        connection_id: UUID,
        thread_id: str,
        max_messages: int = 10,
    ) -> dict[str, Any]:
        if not isinstance(thread_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,255}",
            thread_id,
        ):
            raise ValueError("Invalid Gmail thread ID.")

        if not 1 <= max_messages <= self.MAX_THREAD_MESSAGES:
            raise ValueError(
                f"max_messages must be between 1 and "
                f"{self.MAX_THREAD_MESSAGES}."
            )

        self._validate_connection(
            user_id=user_id,
            connection_id=connection_id,
            required_scope=self.REQUIRED_READ_SCOPE,
        )

        thread = await self._google_get(
            user_id=user_id,
            connection_id=connection_id,
            endpoint=f"threads/{thread_id}",
            params={"format": "full"},
        )

        response_size = len(
            json.dumps(
                thread,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        if response_size > self.MAX_THREAD_RESPONSE_BYTES:
            raise ValueError(
                "Gmail thread is too large to process safely."
            )

        raw_messages = thread.get("messages", [])
        if not isinstance(raw_messages, list):
            raise ValueError("Gmail returned an invalid thread.")

        messages = []
        for raw_message in raw_messages[:max_messages]:
            if not isinstance(raw_message, dict):
                continue
            message = GmailEmailParser.parse_message(raw_message)
            body = message.get("body", {})
            for body_key in ("plain_text", "html"):
                value = body.get(body_key)
                if isinstance(value, str):
                    body[body_key] = value[:self.MAX_THREAD_BODY_CHARS]
            messages.append(message)

        return {
            "id": thread.get("id", thread_id),
            "messages": messages,
            "truncated": len(raw_messages) > max_messages,
        }


    async def get_attachment(
        self,
        user_id: UUID,
        connection_id: UUID,
        message_id: str,
        attachment_id: str,
    ) -> dict[str, Any]:

        if not isinstance(message_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]+",
            message_id,
        ):
            raise ValueError("Invalid Gmail message ID.")

        if not isinstance(attachment_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]+",
            attachment_id,
        ):
            raise ValueError("Invalid Gmail attachment ID.")

        self._validate_connection(user_id, connection_id)

        result = await self._google_get(
            user_id=user_id,
            connection_id=connection_id,
            endpoint=(
                f"messages/{message_id}/attachments/{attachment_id}"
            ),
        )

        return {
            "data": result.get("data"),
            "size": result.get("size"),
        }


    async def download_attachment(
        self,
        user_id: UUID,
        connection_id: UUID,
        message_id: str,
        attachment_id: str,
    ) -> bytes:

        attachment = await self.get_attachment(
            user_id=user_id,
            connection_id=connection_id,
            message_id=message_id,
            attachment_id=attachment_id,
        )

        return GmailAttachmentHandler.decode_attachment(
            attachment
        )


    async def send_message(
        self,
        user_id: UUID,
        connection_id: UUID,
        to: list[str],
        subject: str,
        body: str,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        html_body: str | None = None,
        reply_to: str | None = None,
    ) -> dict[str, Any]:

        # Verify connection and Gmail sending permission.
        self._validate_connection(
            user_id=user_id,
            connection_id=connection_id,
            required_scope=self.REQUIRED_SEND_SCOPE,
        )

        # Build Gmail-compatible MIME message.
        raw_message = GmailMessageBuilder.build(
            to=to,
            subject=subject,
            body=body,
            cc=cc,
            bcc=bcc,
            html_body=html_body,
            reply_to=reply_to,
        )

        # Send email through Gmail API.
        result = await self._google_post(
            user_id=user_id,
            connection_id=connection_id,
            endpoint="messages/send",
            json_data={
                "raw": raw_message,
            },
        )

        return {
            "id": result.get("id"),
            "thread_id": result.get("threadId"),
            "label_ids": result.get("labelIds", []),
            "status": "sent",
        }

    async def trash_message(
        self,
        user_id: UUID,
        connection_id: UUID,
        message_id: str,
    ) -> dict[str, Any]:
        if not isinstance(message_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]+",
            message_id,
        ):
            raise ValueError("Invalid Gmail message ID.")

        self._validate_connection(
            user_id=user_id,
            connection_id=connection_id,
            required_scope=self.REQUIRED_MODIFY_SCOPE,
        )

        result = await self._google_post(
            user_id=user_id,
            connection_id=connection_id,
            endpoint=f"messages/{message_id}/trash",
            json_data=None,
        )

        return {
            "id": result.get("id", message_id),
            "label_ids": result.get("labelIds", []),
            "status": "trashed",
        }

    async def restore_message(
        self,
        user_id: UUID,
        connection_id: UUID,
        message_id: str,
    ) -> dict[str, Any]:
        if not isinstance(message_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]+",
            message_id,
        ):
            raise ValueError("Invalid Gmail message ID.")

        self._validate_connection(
            user_id=user_id,
            connection_id=connection_id,
            required_scope=self.REQUIRED_MODIFY_SCOPE,
        )

        result = await self._google_post(
            user_id=user_id,
            connection_id=connection_id,
            endpoint=f"messages/{message_id}/untrash",
            json_data=None,
        )

        return {
            "id": result.get("id", message_id),
            "label_ids": result.get("labelIds", []),
            "status": "restored",
        }

    async def archive_message(
        self,
        user_id: UUID,
        connection_id: UUID,
        message_id: str,
    ) -> dict[str, Any]:
        if not isinstance(message_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]+",
            message_id,
        ):
            raise ValueError("Invalid Gmail message ID.")

        self._validate_connection(
            user_id=user_id,
            connection_id=connection_id,
            required_scope=self.REQUIRED_MODIFY_SCOPE,
        )

        result = await self._google_post(
            user_id=user_id,
            connection_id=connection_id,
            endpoint=f"messages/{message_id}/modify",
            json_data={"removeLabelIds": ["INBOX"]},
        )

        return {
            "id": result.get("id", message_id),
            "label_ids": result.get("labelIds", []),
            "status": "archived",
        }

    async def permanently_delete_message(
        self,
        user_id: UUID,
        connection_id: UUID,
        message_id: str,
    ) -> dict[str, Any]:
        if not isinstance(message_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]+",
            message_id,
        ):
            raise ValueError("Invalid Gmail message ID.")

        self._validate_connection(
            user_id=user_id,
            connection_id=connection_id,
            required_scope=self.REQUIRED_PERMANENT_DELETE_SCOPE,
        )

        message = await self._google_get(
            user_id=user_id,
            connection_id=connection_id,
            endpoint=f"messages/{message_id}",
            params={"format": "metadata"},
        )

        if "TRASH" not in message.get("labelIds", []):
            raise ValueError(
                "Only messages already in Gmail Trash can be permanently deleted."
            )

        await self._google_delete(
            user_id=user_id,
            connection_id=connection_id,
            endpoint=f"messages/{message_id}",
        )

        return {
            "id": message_id,
            "status": "deleted",
        }

    @staticmethod
    def _extract_plain_body(message: dict[str, Any]) -> str:
        """Extract a plain-text body from the parsed Gmail message."""
        body = message.get("body", "")

        if isinstance(body, str):
            return body

        if isinstance(body, dict):
            for key in ("plain", "text", "text/plain", "plain_text"):
                value = body.get(key)
                if isinstance(value, str) and value:
                    return value

        return ""

    @staticmethod
    def _parse_addresses(value: str | None) -> list[str]:
        """Parse email addresses from a Gmail header."""
        if not value:
            return []

        return [
            address.lower()
            for _, address in getaddresses([value])
            if address and "@" in address
        ]

    @staticmethod
    def _add_subject_prefix(subject: str, prefix: str) -> str:
        if subject.lower().startswith(prefix.lower()):
            return subject
        return f"{prefix} {subject}".strip()

    @staticmethod
    def _build_quoted_original(
        headers: dict[str, Any],
        original_body: str,
    ) -> str:
        return (
            "\n\n---------- Original message ----------\n"
            f"From: {headers.get('from', '')}\n"
            f"Date: {headers.get('date', '')}\n"
            f"Subject: {headers.get('subject', '')}\n\n"
            f"{original_body}"
        )

    @staticmethod
    def _build_references(
        existing_references: str | None,
        message_id_header: str,
    ) -> str:
        references = (existing_references or "").strip()
        if message_id_header not in references.split():
            references = f"{references} {message_id_header}".strip()
        return references

    async def _send_threaded_message(
        self,
        user_id: UUID,
        connection_id: UUID,
        to: list[str],
        subject: str,
        body: str,
        thread_id: str,
        in_reply_to: str,
        references: str | None = None,
        cc: list[str] | None = None,
        html_body: str | None = None,
    ) -> dict[str, Any]:
        """Send a reply using Gmail's original thread and MIME headers."""
        self._validate_connection(
            user_id=user_id,
            connection_id=connection_id,
            required_scope=self.REQUIRED_SEND_SCOPE,
        )

        if not thread_id:
            raise ValueError("Original Gmail thread ID is missing.")

        raw_message = GmailMessageBuilder.build(
            to=to,
            cc=cc,
            subject=subject,
            body=body,
            html_body=html_body,
            in_reply_to=in_reply_to,
            references=references,
        )

        result = await self._google_post(
            user_id=user_id,
            connection_id=connection_id,
            endpoint="messages/send",
            json_data={
                "raw": raw_message,
                "threadId": thread_id,
            },
        )

        return {
            "id": result.get("id"),
            "thread_id": result.get("threadId"),
            "label_ids": result.get("labelIds", []),
            "status": "sent",
        }

    async def reply_message(
        self,
        user_id: UUID,
        connection_id: UUID,
        message_id: str,
        body: str,
        html_body: str | None = None,
    ) -> dict[str, Any]:
        """Reply to the original sender, respecting Reply-To when present."""
        self._validate_connection(
            user_id=user_id,
            connection_id=connection_id,
            required_scope=self.REQUIRED_SEND_SCOPE,
        )

        original = await self.get_message(
            user_id=user_id,
            connection_id=connection_id,
            message_id=message_id,
        )
        headers = original.get("headers", {})

        sender = headers.get("reply-to") or headers.get("from")
        recipients = self._parse_addresses(sender)
        if not recipients:
            raise ValueError("Original sender email was not found.")

        original_message_id = headers.get("message-id")
        if not original_message_id:
            raise ValueError("Original Message-ID header is missing.")

        references = self._build_references(
            headers.get("references"),
            original_message_id,
        )
        subject = self._add_subject_prefix(
            headers.get("subject") or "",
            "Re:",
        )
        quoted_body = self._build_quoted_original(
            headers,
            self._extract_plain_body(original),
        )

        return await self._send_threaded_message(
            user_id=user_id,
            connection_id=connection_id,
            to=recipients,
            subject=subject,
            body=body + quoted_body,
            thread_id=original.get("thread_id"),
            in_reply_to=original_message_id,
            references=references,
            html_body=html_body,
        )

    async def reply_all_message(
        self,
        user_id: UUID,
        connection_id: UUID,
        message_id: str,
        body: str,
        html_body: str | None = None,
    ) -> dict[str, Any]:
        """Reply to sender and original To/Cc recipients, excluding self."""
        connection = self._validate_connection(
            user_id=user_id,
            connection_id=connection_id,
            required_scope=self.REQUIRED_SEND_SCOPE,
        )

        original = await self.get_message(
            user_id=user_id,
            connection_id=connection_id,
            message_id=message_id,
        )
        headers = original.get("headers", {})

        sender = headers.get("reply-to") or headers.get("from")
        sender_addresses = self._parse_addresses(sender)
        if not sender_addresses:
            raise ValueError("Original sender email was not found.")

        own_email = (getattr(connection, "email", None) or "").lower()
        to_recipients: list[str] = []
        cc_recipients: list[str] = []

        for address in sender_addresses:
            if address != own_email and address not in to_recipients:
                to_recipients.append(address)

        original_recipients = (
            self._parse_addresses(headers.get("to"))
            + self._parse_addresses(headers.get("cc"))
        )
        for address in original_recipients:
            if (
                address != own_email
                and address not in to_recipients
                and address not in cc_recipients
            ):
                cc_recipients.append(address)

        if not to_recipients:
            raise ValueError("No valid Reply All recipients found.")

        original_message_id = headers.get("message-id")
        if not original_message_id:
            raise ValueError("Original Message-ID header is missing.")

        references = self._build_references(
            headers.get("references"),
            original_message_id,
        )
        subject = self._add_subject_prefix(
            headers.get("subject") or "",
            "Re:",
        )
        quoted_body = self._build_quoted_original(
            headers,
            self._extract_plain_body(original),
        )

        return await self._send_threaded_message(
            user_id=user_id,
            connection_id=connection_id,
            to=to_recipients,
            cc=cc_recipients,
            subject=subject,
            body=body + quoted_body,
            thread_id=original.get("thread_id"),
            in_reply_to=original_message_id,
            references=references,
            html_body=html_body,
        )

    async def forward_message(
        self,
        user_id: UUID,
        connection_id: UUID,
        message_id: str,
        to: list[str],
        body: str = "",
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
    ) -> dict[str, Any]:
        """Forward an email as a new message with its original text quoted."""
        self._validate_connection(
            user_id=user_id,
            connection_id=connection_id,
            required_scope=self.REQUIRED_SEND_SCOPE,
        )

        original = await self.get_message(
            user_id=user_id,
            connection_id=connection_id,
            message_id=message_id,
        )
        headers = original.get("headers", {})
        original_body = self._extract_plain_body(original)

        forwarded_content = (
            f"{body}\n\n"
            "---------- Forwarded message ----------\n"
            f"From: {headers.get('from', '')}\n"
            f"Date: {headers.get('date', '')}\n"
            f"Subject: {headers.get('subject', '')}\n"
            f"To: {headers.get('to', '')}\n\n"
            f"{original_body}"
        )
        subject = self._add_subject_prefix(
            headers.get("subject") or "",
            "Fwd:",
        )

        return await self.send_message(
            user_id=user_id,
            connection_id=connection_id,
            to=to,
            cc=cc,
            bcc=bcc,
            subject=subject,
            body=forwarded_content,
        )
