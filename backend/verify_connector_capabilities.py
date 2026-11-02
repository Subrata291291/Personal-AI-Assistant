
from app.integrations.connectors.base import BaseConnector
from app.integrations.connectors.registry import ConnectorRegistry
from app.integrations.connectors.schemas import (
    ConnectorCapability,
    CapabilityRisk,
)


class DummyGmailConnector(BaseConnector):
    provider = "google_gmail"

    async def connect(self, user_id: str) -> dict:
        return {"status": "connected"}

    async def disconnect(self, user_id: str) -> dict:
        return {"status": "disconnected"}

    async def get_status(self, user_id: str) -> dict:
        return {"status": "connected"}

    async def get_capabilities(self) -> list[str]:
        return ["search_emails", "send_email"]


registry = ConnectorRegistry()
gmail = DummyGmailConnector()

capabilities = [
    ConnectorCapability(
        name="search_emails",
        provider="google_gmail",
        description="Search Gmail messages.",
        risk=CapabilityRisk.READ,
        required_permissions={"gmail.read"},
    ),
    ConnectorCapability(
        name="send_email",
        provider="google_gmail",
        description="Send an email.",
        risk=CapabilityRisk.WRITE,
        required_permissions={"gmail.send"},
        requires_approval=True,
    ),
]

registry.register(gmail, capabilities)

# 1. Connector registration
assert registry.is_registered("google_gmail")
print("PASS: Connector registration")

# 2. Capability listing
assert registry.list_capabilities("google_gmail") == [
    "search_emails",
    "send_email",
]
print("PASS: Capability listing")

# 3. Capability retrieval
send_email = registry.get_capability(
    "GOOGLE_GMAIL",
    "SEND_EMAIL",
)
assert send_email.requires_approval is True
print("PASS: Capability retrieval")

# 4. Capability provider mismatch
try:
    invalid = ConnectorCapability(
        name="search_files",
        provider="google_drive",
        description="Search Drive files.",
        risk=CapabilityRisk.READ,
    )
    registry.register(
        DummyGmailConnector(),
        [invalid],
    )
    raise AssertionError("Provider mismatch was accepted.")
except ValueError:
    print("PASS: Capability provider mismatch rejected")

# 5. Unknown capability
try:
    registry.get_capability(
        "google_gmail",
        "delete_email",
    )
    raise AssertionError("Unknown capability was accepted.")
except ValueError:
    print("PASS: Unknown capability rejected")

print("\nAll 5 manual verification checks passed.")
