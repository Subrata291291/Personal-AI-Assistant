import base64
from typing import Any


class GmailEmailParser:

    @staticmethod
    def decode_body(data: str | None) -> str:
        if not data:
            return ""

        try:
            padding = "=" * (-len(data) % 4)

            decoded = base64.urlsafe_b64decode(
                data + padding
            )

            return decoded.decode("utf-8", errors="replace")

        except (ValueError, UnicodeError):
            return ""

    @classmethod
    def extract_bodies(
        cls,
        payload: dict[str, Any],
    ) -> dict[str, str]:

        result = {
            "plain_text": "",
            "html": "",
        }

        def walk(part: dict[str, Any]) -> None:

            mime_type = part.get("mimeType", "")
            body = part.get("body", {})
            data = body.get("data")

            if data:
                decoded = cls.decode_body(data)

                if mime_type == "text/plain":
                    result["plain_text"] += decoded

                elif mime_type == "text/html":
                    result["html"] += decoded

            for child in part.get("parts", []):
                walk(child)

        walk(payload)

        return result

    @staticmethod
    def extract_headers(
        payload: dict[str, Any],
    ) -> dict[str, str]:

        headers = {}

        for header in payload.get("headers", []):
            name = header.get("name", "").lower()
            value = header.get("value", "")

            if name in {"from", "to", "subject", "date", "cc"}:
                headers[name] = value

        return headers

    @classmethod
    def parse_message(
        cls,
        message: dict[str, Any],
    ) -> dict[str, Any]:

        payload = message.get("payload", {})

        bodies = cls.extract_bodies(payload)
        headers = cls.extract_headers(payload)

        return {
            "id": message.get("id"),
            "thread_id": message.get("threadId"),
            "snippet": message.get("snippet", ""),
            "internal_date": message.get("internalDate"),
            "headers": headers,
            "body": bodies,
            "labels": message.get("labelIds", []),
        }


    @classmethod
    def extract_attachments(
        cls,
        payload: dict[str, Any],
    ) -> list[dict[str, Any]]:

        attachments = []

        def walk(part: dict[str, Any]) -> None:

            filename = part.get("filename", "")
            body = part.get("body", {})

            attachment_id = body.get("attachmentId")

            if filename and attachment_id:
                attachments.append({
                    "filename": filename,
                    "mime_type": part.get("mimeType", ""),
                    "attachment_id": attachment_id,
                    "size": body.get("size", 0),
                })

            for child in part.get("parts", []):
                walk(child)

        walk(payload)

        return attachments
    

    @staticmethod
    def parse_message(
        message: dict[str, Any],
    ) -> dict[str, Any]:

        payload = message.get("payload", {})

        bodies = GmailEmailParser.extract_bodies(payload)
        headers = GmailEmailParser.extract_headers(payload)
        attachments = GmailEmailParser.extract_attachments(payload)

        return {
            "id": message.get("id"),
            "thread_id": message.get("threadId"),
            "snippet": message.get("snippet", ""),
            "internal_date": message.get("internalDate"),
            "headers": headers,
            "body": bodies,
            "labels": message.get("labelIds", []),
            "attachments": attachments,
        }