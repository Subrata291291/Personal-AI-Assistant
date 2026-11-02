import base64
import binascii
from typing import Any


class GmailAttachmentHandler:

    MAX_ATTACHMENT_SIZE = 10 * 1024 * 1024  # 10 MB

    @classmethod
    def decode_attachment(
        cls,
        attachment: dict[str, Any],
    ) -> bytes:

        encoded_data = attachment.get("data")

        if not isinstance(encoded_data, str) or not encoded_data:
            raise ValueError("Attachment data is missing.")

        declared_size = attachment.get("size")

        if (
            not isinstance(declared_size, int)
            or isinstance(declared_size, bool)
            or declared_size < 0
        ):
            raise ValueError("Invalid attachment size.")

        if declared_size > cls.MAX_ATTACHMENT_SIZE:
            raise ValueError("Attachment exceeds the 10 MB limit.")

        # Reject excessively large encoded input before decoding.
        max_encoded_size = (
            (cls.MAX_ATTACHMENT_SIZE + 2) // 3
        ) * 4

        if len(encoded_data) > max_encoded_size:
            raise ValueError("Encoded attachment is too large.")

        try:
            padding = "=" * (-len(encoded_data) % 4)

            decoded_data = base64.b64decode(
                encoded_data + padding,
                altchars=b"-_",
                validate=True,
            )

        except (ValueError, binascii.Error) as exc:
            raise ValueError("Invalid Base64URL attachment data.") from exc

        if len(decoded_data) != declared_size:
            raise ValueError("Attachment size validation failed.")

        return decoded_data