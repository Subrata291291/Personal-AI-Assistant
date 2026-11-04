
import base64
from typing import Any


class GmailEmailParser:
    """
    Parse Gmail API message payloads.

    Supports:
    - Nested multipart messages
    - text/plain
    - text/html
    - multipart/alternative
    - multipart/related
    - Attachments
    - Inline attachments
    """

    @staticmethod
    def decode_body(
        data: str | None,
        charset: str = "utf-8",
        max_chars: int | None = None,
    ) -> str:
        if not data:
            return ""

        try:
            if max_chars is not None:
                if max_chars <= 0:
                    return ""
                encoded_limit = (max_chars * 16 + 2) // 3
                encoded_limit -= encoded_limit % 4
                data = data[:encoded_limit]
                if not data:
                    return ""

            padding = "=" * (-len(data) % 4)

            decoded = base64.urlsafe_b64decode(
                data + padding
            )

            try:
                text = decoded.decode(
                    charset or "utf-8",
                    errors="replace",
                )
            except LookupError:
                text = decoded.decode(
                    "utf-8",
                    errors="replace",
                )
            return text[:max_chars] if max_chars is not None else text

        except (ValueError, UnicodeError):
            return ""

    @staticmethod
    def _get_charset(part: dict[str, Any]) -> str:
        for header in part.get("headers", []):
            if header.get("name", "").lower() == "content-type":
                value = header.get("value", "")

                for item in value.split(";"):
                    item = item.strip()

                    if item.lower().startswith("charset="):
                        return item.split("=", 1)[1].strip(
                            '"\' '
                        )

        return "utf-8"

    @classmethod
    def extract_bodies(
        cls,
        payload: dict[str, Any],
        include_html: bool = True,
        max_body_chars: int | None = None,
    ) -> dict[str, str]:

        result = {
            "plain_text": "",
            "html": "",
        }

        plain_parts = []
        html_parts = []
        plain_chars = 0
        html_chars = 0

        def walk(part: dict[str, Any]) -> None:
            nonlocal html_chars, plain_chars

            mime_type = part.get("mimeType", "").lower()

            filename = part.get("filename", "")
            body = part.get("body", {})
            data = body.get("data")

            # Do not treat file attachments as email body content.
            if filename:
                return

            if data and (
                mime_type == "text/plain"
                or (include_html and mime_type == "text/html")
            ):
                remaining_chars = None
                if max_body_chars is not None:
                    used_chars = (
                        plain_chars
                        if mime_type == "text/plain"
                        else html_chars
                    )
                    remaining_chars = max(0, max_body_chars - used_chars)
                decoded = (
                    cls.decode_body(
                        data,
                        cls._get_charset(part),
                        max_chars=remaining_chars,
                    )
                    if remaining_chars is None or remaining_chars > 0
                    else ""
                )

                if decoded:
                    if mime_type == "text/plain":
                        plain_parts.append(decoded)
                        plain_chars += len(decoded)

                    elif mime_type == "text/html":
                        html_parts.append(decoded)
                        html_chars += len(decoded)

            for child in part.get("parts", []):
                walk(child)

        walk(payload)

        result["plain_text"] = "\n\n".join(
            item.strip()
            for item in plain_parts
            if item.strip()
        )

        result["html"] = "\n".join(
            html_parts
        )

        if max_body_chars is not None:
            result["plain_text"] = result["plain_text"][:max_body_chars]
            result["html"] = result["html"][:max_body_chars]

        return result

    @staticmethod
    def extract_headers(
        payload: dict[str, Any],
    ) -> dict[str, str]:

        headers = {}

        supported_headers = {
            "from",
            "to",
            "subject",
            "date",
            "cc",
            "bcc",
            "reply-to",
            "message-id",
            "in-reply-to",
            "references",
        }

        for header in payload.get("headers", []):
            name = header.get("name", "").lower()
            value = header.get("value", "")

            if name in supported_headers:
                headers[name] = value

        return headers

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

            if filename:
                attachments.append({
                    "filename": filename,
                    "mime_type": part.get("mimeType", ""),
                    "attachment_id": attachment_id,
                    "size": body.get("size", 0),
                    "is_inline": any(
                        header.get("name", "").lower()
                        == "content-id"
                        for header in part.get("headers", [])
                    ),
                })

            for child in part.get("parts", []):
                walk(child)

        walk(payload)

        return attachments

    @classmethod
    def parse_message(
        cls,
        message: dict[str, Any],
        include_html: bool = True,
        max_body_chars: int | None = None,
    ) -> dict[str, Any]:

        payload = message.get("payload", {})

        bodies = cls.extract_bodies(
            payload,
            include_html=include_html,
            max_body_chars=max_body_chars,
        )
        headers = cls.extract_headers(payload)
        attachments = cls.extract_attachments(payload)

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
