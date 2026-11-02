from app.integrations.connectors.schemas import (
    ConnectorCapability,
    CapabilityRisk,
)


GOOGLE_GMAIL_CAPABILITIES = [
    ConnectorCapability(
        name="list_emails",
        provider="google",
        description="Retrieve a paginated list of Gmail message IDs.",
        risk=CapabilityRisk.READ,
        required_permissions={
            "https://www.googleapis.com/auth/gmail.readonly",
        },
        requires_approval=False,
    ),
]