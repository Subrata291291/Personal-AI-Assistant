import base64

from app.integrations.google.email_parser import GmailEmailParser


def encode_body(text: str) -> str:
    return base64.urlsafe_b64encode(
        text.encode("utf-8")
    ).decode("utf-8").rstrip("=")


sample_message = {
    "id": "message_123",
    "threadId": "thread_456",
    "snippet": "Hello, this is a test email.",
    "internalDate": "1791000000000",
    "labelIds": ["INBOX", "UNREAD"],
    "payload": {
        "mimeType": "multipart/mixed",
        "headers": [
            {"name": "From", "value": "sender@example.com"},
            {"name": "To", "value": "user@example.com"},
            {"name": "Subject", "value": "Test Email"},
            {"name": "Date", "value": "Mon, 2 Nov 2026"},
        ],
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    {
                        "mimeType": "text/plain",
                        "body": {
                            "data": encode_body("Hello from plain text.")
                        },
                    },
                    {
                        "mimeType": "text/html",
                        "body": {
                            "data": encode_body("<p>Hello from HTML.</p>")
                        },
                    },
                ],
            },
            {
                "mimeType": "application/pdf",
                "filename": "invoice.pdf",
                "body": {
                    "attachmentId": "attachment_123",
                    "size": 2048,
                },
            },
        ],
    },
}


result = GmailEmailParser.parse_message(sample_message)

assert result["id"] == "message_123"
assert result["thread_id"] == "thread_456"
assert result["headers"]["from"] == "sender@example.com"
assert result["headers"]["subject"] == "Test Email"

assert result["body"]["plain_text"] == "Hello from plain text."
assert result["body"]["html"] == "<p>Hello from HTML.</p>"

assert "INBOX" in result["labels"]

assert len(result["attachments"]) == 1

attachment = result["attachments"][0]

assert attachment["filename"] == "invoice.pdf"
assert attachment["mime_type"] == "application/pdf"
assert attachment["attachment_id"] == "attachment_123"
assert attachment["size"] == 2048

print("Email Parser functional verification: PASSED")
print("Message ID:", result["id"])
print("Subject:", result["headers"]["subject"])
print("Plain text:", result["body"]["plain_text"])
print("HTML:", result["body"]["html"])
print("Attachment metadata extraction: PASSED")
print("Attachment filename:", attachment["filename"])
print("Attachment MIME type:", attachment["mime_type"])
print("Headers and labels: OK")