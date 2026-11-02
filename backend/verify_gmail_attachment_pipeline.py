
import asyncio
import base64
from pathlib import Path
from unittest.mock import AsyncMock

from app.services.gmail_attachment_service import (
    GmailAttachmentService,
)


async def main():

    service = GmailAttachmentService()

    # Mock Gmail API response with a sample PDF.
    pdf_content = b"%PDF-1.7\nSample Gmail PDF content"

    encoded_data = base64.urlsafe_b64encode(
        pdf_content
    ).decode("utf-8").rstrip("=")

    service.gmail_connector.get_attachment = AsyncMock(
        return_value={
            "data": encoded_data,
            "size": len(pdf_content),
        }
    )

    user_id = "11111111-1111-4111-8111-111111111111"
    connection_id = "22222222-2222-4222-8222-222222222222"

    # Complete attachment pipeline.
    result = await service.download_attachment(
        db=None,
        user_id=user_id,
        connection_id=connection_id,
        message_id="message_123",
        attachment_id="attachment_123",
        filename="invoice.pdf",
        mime_type="application/pdf",
    )

    file_path = Path(result["temporary_path"])

    try:
        assert file_path.exists()
        assert file_path.read_bytes() == pdf_content
        assert result["filename"] == "invoice.pdf"
        assert result["mime_type"] == "application/pdf"
        assert result["size"] == len(pdf_content)

        print("Attachment decoding: PASSED")
        print("PDF signature validation: PASSED")
        print("Temporary file creation: PASSED")
        print("File content integrity: PASSED")

    finally:
        service.cleanup_attachment(str(file_path))

    assert not file_path.exists()

    print("Temporary file cleanup: PASSED")

    # Verify fake PDF rejection.
    fake_pdf = b"This is not a real PDF."

    fake_encoded = base64.urlsafe_b64encode(
        fake_pdf
    ).decode("utf-8").rstrip("=")

    service.gmail_connector.get_attachment = AsyncMock(
        return_value={
            "data": fake_encoded,
            "size": len(fake_pdf),
        }
    )

    try:
        await service.download_attachment(
            db=None,
            user_id=user_id,
            connection_id=connection_id,
            message_id="message_123",
            attachment_id="attachment_456",
            filename="fake.pdf",
            mime_type="application/pdf",
        )

    except ValueError:
        print("Fake PDF rejection: PASSED")

    else:
        raise AssertionError("Fake PDF was accepted.")

    print("Complete Gmail Attachment Pipeline: PASSED")


asyncio.run(main())
