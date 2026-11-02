import tempfile
from pathlib import Path
from uuid import uuid4

from app.services.gmail_attachment_service import (
    GmailAttachmentService,
)


service = GmailAttachmentService()

# Create a temporary attachment file.
temp_directory = Path(tempfile.gettempdir())

filename = f"pa_gmail_{uuid4().hex}.bin"
file_path = temp_directory / filename

sample_data = b"Test Gmail attachment content."

with open(file_path, "xb") as file:
    file.write(sample_data)

assert file_path.exists()
assert file_path.read_bytes() == sample_data

print("Temporary file creation: PASSED")
print("File content verification: PASSED")

# Cleanup the temporary file.
service.cleanup_attachment(str(file_path))

assert not file_path.exists()

print("Temporary file cleanup: PASSED")


# Verify invalid path rejection.
try:
    service.cleanup_attachment(
        str(temp_directory / "invalid_file.txt")
    )
except ValueError:
    print("Invalid path rejection: PASSED")
else:
    raise AssertionError("Invalid path was accepted.")


print("Gmail Attachment Service verification: PASSED")