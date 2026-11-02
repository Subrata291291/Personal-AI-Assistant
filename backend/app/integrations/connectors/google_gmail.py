
import re
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.integrations.google.api_client import GoogleAPIClient
from app.integrations.connectors.base import BaseConnector


class GoogleGmailConnector(BaseConnector):

    provider = "google"

    GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1"

    def __init__(self):
        self.api_client = GoogleAPIClient()

    @staticmethod
    def _validate_resource_id(
        value: str,
        field_name: str,
        max_length: int,
    ) -> None:

        if not isinstance(value, str):
            raise ValueError(f"Invalid {field_name}.")

        if not 1 <= len(value) <= max_length:
            raise ValueError(f"Invalid {field_name} length.")

        if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            raise ValueError(f"Invalid characters in {field_name}.")

    async def connect(self, user_id: str) -> dict[str, Any]:
        UUID(user_id)

        return {
            "provider": self.provider,
            "user_id": user_id,
            "status": "OAuth connection required",
        }

    async def disconnect(self, user_id: str) -> dict[str, Any]:
        UUID(user_id)

        return {
            "provider": self.provider,
            "user_id": user_id,
            "message": "Disconnect must be handled by UserConnectionService.",
        }

    async def get_status(self, user_id: str) -> dict[str, Any]:
        UUID(user_id)

        return {
            "provider": self.provider,
            "user_id": user_id,
        }

    async def get_capabilities(self) -> list[str]:
        return [
            "list_emails",
        ]

    async def list_emails(
        self,
        db: Session,
        user_id: str,
        connection_id: str,
        max_results: int = 10,
        page_token: str | None = None,
    ) -> dict[str, Any]:

        UUID(user_id)
        UUID(connection_id)

        if not 1 <= max_results <= 100:
            raise ValueError("max_results must be between 1 and 100.")

        params: dict[str, Any] = {
            "maxResults": max_results,
        }

        if page_token:
            if len(page_token) > 2048:
                raise ValueError("Invalid Gmail page token.")

            params["pageToken"] = page_token

        return await self.api_client.request(
            db=db,
            user_id=user_id,
            connection_id=connection_id,
            method="GET",
            url=f"{self.GMAIL_API_BASE}/users/me/messages",
            params=params,
        )

    async def get_email(
        self,
        db: Session,
        user_id: str,
        connection_id: str,
        message_id: str,
        format: str = "full",
    ) -> dict[str, Any]:

        UUID(user_id)
        UUID(connection_id)

        self._validate_resource_id(
            message_id,
            "message ID",
            255,
        )

        allowed_formats = {
            "minimal",
            "metadata",
            "full",
        }

        if format not in allowed_formats:
            raise ValueError("Unsupported Gmail message format.")

        return await self.api_client.request(
            db=db,
            user_id=user_id,
            connection_id=connection_id,
            method="GET",
            url=(
                f"{self.GMAIL_API_BASE}/users/me/messages/"
                f"{message_id}"
            ),
            params={"format": format},
        )

    async def search_emails(
        self,
        db: Session,
        user_id: str,
        connection_id: str,
        query: str,
        max_results: int = 10,
        page_token: str | None = None,
    ) -> dict[str, Any]:

        UUID(user_id)
        UUID(connection_id)

        if not query or not query.strip():
            raise ValueError("Gmail search query cannot be empty.")

        if len(query) > 500:
            raise ValueError("Gmail search query is too long.")

        if not 1 <= max_results <= 100:
            raise ValueError("max_results must be between 1 and 100.")

        params: dict[str, Any] = {
            "q": query.strip(),
            "maxResults": max_results,
        }

        if page_token:
            if len(page_token) > 2048:
                raise ValueError("Invalid Gmail page token.")

            params["pageToken"] = page_token

        return await self.api_client.request(
            db=db,
            user_id=user_id,
            connection_id=connection_id,
            method="GET",
            url=f"{self.GMAIL_API_BASE}/users/me/messages",
            params=params,
        )

    async def get_attachment(
        self,
        db: Session,
        user_id: str,
        connection_id: str,
        message_id: str,
        attachment_id: str,
    ) -> dict[str, Any]:

        UUID(user_id)
        UUID(connection_id)

        self._validate_resource_id(
            message_id,
            "message ID",
            255,
        )

        self._validate_resource_id(
            attachment_id,
            "attachment ID",
            1024,
        )

        return await self.api_client.request(
            db=db,
            user_id=user_id,
            connection_id=connection_id,
            method="GET",
            url=(
                f"{self.GMAIL_API_BASE}/users/me/messages/"
                f"{message_id}/attachments/{attachment_id}"
            ),
        )
