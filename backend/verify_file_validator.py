
from app.integrations.google.file_validator import (
    AttachmentFileValidator,
)


validator = AttachmentFileValidator


# Valid PDF
pdf_data = b"%PDF-1.7\nTest PDF content"

result = validator.validate(
    file_bytes=pdf_data,
    filename="invoice.pdf",
    declared_mime_type="application/pdf",
)

assert result["signature_valid"] is True
assert result["extension"] == ".pdf"

print("Valid PDF detection: PASSED")


# Valid PNG
png_data = b"\x89PNG\r\n\x1a\n" + b"test-image-content"

result = validator.validate(
    file_bytes=png_data,
    filename="image.png",
    declared_mime_type="image/png",
)

assert result["signature_valid"] is True

print("Valid PNG detection: PASSED")


# MIME mismatch
try:
    validator.validate(
        file_bytes=pdf_data,
        filename="invoice.png",
        declared_mime_type="image/png",
    )
except ValueError:
    print("MIME mismatch rejection: PASSED")
else:
    raise AssertionError("MIME mismatch was accepted.")


# Unsupported MIME type
try:
    validator.validate(
        file_bytes=b"test",
        filename="file.exe",
        declared_mime_type="application/x-msdownload",
    )
except ValueError:
    print("Unsupported file rejection: PASSED")
else:
    raise AssertionError("Unsupported MIME type was accepted.")


# Oversized file
try:
    validator.validate(
        file_bytes=b"A" * (11 * 1024 * 1024),
        filename="large.pdf",
        declared_mime_type="application/pdf",
    )
except ValueError:
    print("Oversized file rejection: PASSED")
else:
    raise AssertionError("Oversized file was accepted.")


print("File Validator verification: PASSED")
