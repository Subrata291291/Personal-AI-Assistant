
from app.integrations.connectors.permissions.approval_policy import (
    ApprovalPolicy,
)

policy = ApprovalPolicy()

# 1. Send email
result = policy.check_approval("send_email")
assert result.required is True
print("PASS: Send email requires approval")

# 2. Delete email
result = policy.check_approval("delete_email")
assert result.required is True
print("PASS: Delete email requires approval")

# 3. Create calendar event
result = policy.check_approval("create_calendar_event")
assert result.required is True
print("PASS: Create calendar event requires approval")

# 4. Search emails
result = policy.check_approval("search_emails")
assert result.required is False
print("PASS: Search emails does not require approval")

# 5. Read emails
result = policy.check_approval("read_emails")
assert result.required is False
print("PASS: Read emails does not require approval")

# 6. Empty capability
try:
    policy.check_approval("")
    raise AssertionError("Empty capability was accepted.")
except ValueError:
    print("PASS: Empty capability rejected")

print("\nAll 6 manual verification checks passed.")
