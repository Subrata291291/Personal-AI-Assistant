
from app.integrations.connectors.permissions.manager import (
    PermissionManager,
)


manager = PermissionManager()

granted_permissions = {
    "search_emails",
    "read_emails",
}

# 1. Granted permission
result = manager.check_permission(
    "search_emails",
    granted_permissions,
)
assert result.allowed is True
print("PASS: Granted permission accepted")

# 2. Missing permission
result = manager.check_permission(
    "send_email",
    granted_permissions,
)
assert result.allowed is False
print("PASS: Missing permission rejected")

# 3. Case-insensitive permission
result = manager.check_permission(
    "SEARCH_EMAILS",
    granted_permissions,
)
assert result.allowed is True
print("PASS: Case-insensitive permission")

# 4. Empty capability
result = manager.check_permission(
    "",
    granted_permissions,
)
assert result.allowed is False
print("PASS: Empty capability rejected")

# 5. Whitespace-only capability
result = manager.check_permission(
    "   ",
    granted_permissions,
)
assert result.allowed is False
print("PASS: Whitespace capability rejected")

print("\nAll 5 manual verification checks passed.")
