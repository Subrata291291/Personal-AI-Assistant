import os
import tempfile
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.integrations.connectors.google_gmail import (
    GoogleGmailConnector,
)
from app.integrations.google.attachment_handler import (
    GmailAttachmentHandler,
)
from app.integrations.google.file_validator import (
    AttachmentFileValidator,
)

class GmailAttachmentService:

    TEMP_PREFIX = "pa_gmail_"

    def __init__(self):
        self.gmail_connector = GoogleGmailConnector()
        self.attachment_handler = GmailAttachmentHandler()
        self.file_validator = AttachmentFileValidator()

    async def download_attachment(
        self,
        db: Session,
        user_id: str,
        connection_id: str,
        message_id: str,
        attachment_id: str,
        filename: str,
        mime_type: str,
    ) -> dict[str, Any]:

        UUID(user_id)
        UUID(connection_id)

        if not filename or len(filename) > 255:
            raise ValueError("Invalid attachment filename.")

        if not mime_type or len(mime_type) > 255:
            raise ValueError("Invalid MIME type.")

        attachment_response = (
            await self.gmail_connector.get_attachment(
                db=db,
                user_id=user_id,
                connection_id=connection_id,
                message_id=message_id,
                attachment_id=attachment_id,
            )
        )

        file_bytes = self.attachment_handler.decode_attachment(
            attachment_response
        )
        self.file_validator.validate(
            file_bytes=file_bytes,
            filename=filename,
            declared_mime_type=mime_type,
        )

        # Never use the original filename as a filesystem path.
        temp_directory = Path(tempfile.gettempdir())

        temp_filename = f"{self.TEMP_PREFIX}{uuid4().hex}.bin"

        file_path = temp_directory / temp_filename

        try:
            with open(file_path, "xb") as file:
                file.write(file_bytes)

        except Exception:
            file_path.unlink(missing_ok=True)
            raise

        return {
            "filename": filename,
            "mime_type": mime_type,
            "size": len(file_bytes),
            "temporary_path": str(file_path),
        }

    @staticmethod
    def cleanup_attachment(
        temporary_path: str,
    ) -> None:

        path = Path(temporary_path).resolve()
        temp_directory = Path(tempfile.gettempdir()).resolve()

        if path.parent != temp_directory:
            raise ValueError("Invalid temporary attachment path.")

        if not path.name.startswith(
            GmailAttachmentService.TEMP_PREFIX
        ):
            raise ValueError("Unrecognized temporary attachment.")

        if path.is_symlink():
            raise ValueError("Symbolic links are not allowed.")

        path.unlink(missing_ok=True)