
from app.integrations.connectors.base import BaseConnector
from app.integrations.connectors.registry import ConnectorRegistry


class DummyGmailConnector(BaseConnector):
    provider = "google_gmail"

    async def connect(self, user_id: str) -> dict:
        return {"status": "connected", "user_id": user_id}

    async def disconnect(self, user_id: str) -> dict:
        return {"status": "disconnected", "user_id": user_id}

    async def get_status(self, user_id: str) -> dict:
        return {"status": "connected", "user_id": user_id}

    async def get_capabilities(self) -> list[str]:
        return ["search_emails", "get_email"]


registry = ConnectorRegistry()
gmail = DummyGmailConnector()

# 1. Register connector
registry.register(gmail)
assert registry.is_registered("google_gmail")
print("PASS: Connector registration")

# 2. Retrieve connector
retrieved = registry.get("GOOGLE_GMAIL")
assert retrieved is gmail
print("PASS: Connector retrieval")

# 3. List providers
assert registry.list_providers() == ["google_gmail"]
print("PASS: Provider listing")

# 4. Reject duplicate registration
try:
    registry.register(gmail)
    raise AssertionError("Duplicate registration was accepted.")
except ValueError:
    print("PASS: Duplicate registration rejected")

# 5. Reject unknown provider
try:
    registry.get("google_calendar")
    raise AssertionError("Unknown provider was accepted.")
except ValueError:
    print("PASS: Unknown connector rejected")

print("\nAll 5 manual verification checks passed.")
