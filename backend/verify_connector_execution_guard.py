
from app.integrations.connectors.permissions.execution_guard import (
    ConnectorExecutionGuard,
)

guard = ConnectorExecutionGuard()

permissions = {
    "search_emails",
    "send_email",
    "delete_email",
}

# 1. Read action with permission
result = guard.authorize("search_emails", permissions)
assert result.allowed is True
print("PASS: Authorized read action")

# 2. Read action without permission
result = guard.authorize("read_emails", set())
assert result.allowed is False
print("PASS: Unauthorized action blocked")

# 3. Send email without approval
result = guard.authorize("send_email", permissions)
assert result.allowed is False
print("PASS: Send email blocked without approval")

# 4. Send email with approval
result = guard.authorize(
    "send_email",
    permissions,
    approval_granted=True,
)
assert result.allowed is True
print("PASS: Send email allowed with approval")

# 5. Delete email without approval
result = guard.authorize("delete_email", permissions)
assert result.allowed is False
print("PASS: Delete email blocked without approval")

# 6. Delete email with approval
result = guard.authorize(
    "delete_email",
    permissions,
    approval_granted=True,
)
assert result.allowed is True
print("PASS: Delete email allowed with approval")

print("\nAll 6 manual verification checks passed.")
