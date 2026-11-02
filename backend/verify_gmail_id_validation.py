from app.integrations.connectors.google_gmail import (
    GoogleGmailConnector,
)


validator = GoogleGmailConnector._validate_resource_id

# Valid IDs
validator("abc123456", "message ID", 255)
validator("attachment_123-ABC", "attachment ID", 1024)

print("Valid ID acceptance: PASSED")


# Invalid IDs
invalid_ids = [
    "../admin",
    "message/123",
    "hello world",
    "abc?test=1",
    "",
]

for value in invalid_ids:
    try:
        validator(value, "message ID", 255)
    except ValueError:
        continue
    else:
        raise AssertionError(
            f"Invalid ID was accepted: {value}"
        )

print("Invalid ID rejection: PASSED")


# Length validation
try:
    validator("a" * 256, "message ID", 255)
except ValueError:
    print("Maximum length validation: PASSED")
else:
    raise AssertionError("Oversized ID was accepted.")


print("Gmail ID security verification: PASSED")