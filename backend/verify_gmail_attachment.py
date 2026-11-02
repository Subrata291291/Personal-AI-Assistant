import base64

from app.integrations.google.attachment_handler import (
    GmailAttachmentHandler,
)


original_data = b"Hello, this is a Gmail attachment."

encoded_data = base64.urlsafe_b64encode(
    original_data
).decode("utf-8").rstrip("=")

sample_attachment = {
    "data": encoded_data,
    "size": len(original_data),
}

decoded = GmailAttachmentHandler.decode_attachment(
    sample_attachment
)

assert decoded == original_data

print("Attachment decoding: PASSED")
print("Decoded content:", decoded.decode("utf-8"))
print("Size validation: PASSED")


# Verify oversized attachment rejection.
try:
    GmailAttachmentHandler.decode_attachment({
        "data": encoded_data,
        "size": 11 * 1024 * 1024,
    })
except ValueError:
    print("Oversized attachment rejection: PASSED")
else:
    raise AssertionError("Oversized attachment was not rejected.")