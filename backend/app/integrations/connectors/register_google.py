from app.integrations.connectors.registry import ConnectorRegistry
from app.integrations.connectors.google_gmail import GoogleGmailConnector
from app.integrations.connectors.google_capabilities import (
    GOOGLE_GMAIL_CAPABILITIES,
)


def register_google_connectors(
    registry: ConnectorRegistry,
) -> None:

    gmail_connector = GoogleGmailConnector()

    registry.register(
        connector=gmail_connector,
        capabilities=GOOGLE_GMAIL_CAPABILITIES,
    )