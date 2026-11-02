
from pathlib import Path


class AttachmentFileValidator:

    MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

    # File signatures (Magic Bytes)
    FILE_SIGNATURES = {
        "application/pdf": [
            (b"%PDF-", 0),
        ],
        "image/png": [
            (b"\x89PNG\r\n\x1a\n", 0),
        ],
        "image/jpeg": [
            (b"\xff\xd8\xff", 0),
        ],
        "image/gif": [
            (b"GIF87a", 0),
            (b"GIF89a", 0),
        ],
        "image/webp": [
            (b"RIFF", 0),
        ],
        "application/zip": [
            (b"PK\x03\x04", 0),
            (b"PK\x05\x06", 0),
            (b"PK\x07\x08", 0),
        ],
    }

    @classmethod
    def validate(
        cls,
        file_bytes: bytes,
        filename: str,
        declared_mime_type: str,
    ) -> dict:

        if not isinstance(file_bytes, bytes):
            raise ValueError("File content must be bytes.")

        if not file_bytes:
            raise ValueError("File is empty.")

        if len(file_bytes) > cls.MAX_FILE_SIZE:
            raise ValueError("File exceeds the 10 MB limit.")

        if not isinstance(filename, str) or not filename.strip():
            raise ValueError("Invalid filename.")

        if len(filename) > 255:
            raise ValueError("Filename is too long.")

        if not isinstance(declared_mime_type, str):
            raise ValueError("Invalid declared MIME type.")

        extension = Path(filename).suffix.lower()

        if not extension:
            raise ValueError("File extension is missing.")

        if declared_mime_type not in cls.FILE_SIGNATURES:
            raise ValueError("Unsupported MIME type.")

        signatures = cls.FILE_SIGNATURES[declared_mime_type]

        signature_matched = any(
            file_bytes.startswith(signature)
            for signature, offset in signatures
            if offset == 0
        )

        if not signature_matched:
            raise ValueError(
                "File content does not match declared MIME type."
            )

        # Additional RIFF/WEBP validation.
        if declared_mime_type == "image/webp":
            if len(file_bytes) < 12 or file_bytes[8:12] != b"WEBP":
                raise ValueError("Invalid WebP signature.")

        return {
            "filename": filename,
            "mime_type": declared_mime_type,
            "extension": extension,
            "size": len(file_bytes),
            "signature_valid": True,
        }
