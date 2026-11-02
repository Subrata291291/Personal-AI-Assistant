
from abc import ABC, abstractmethod
from typing import Any


class BaseConnector(ABC):
    """
    Common interface for all external service connectors.

    Examples:
        GmailConnector
        GoogleCalendarConnector
        GoogleDriveConnector
    """

    provider: str

    @abstractmethod
    async def connect(self, user_id: str) -> dict[str, Any]:
        """Initiate or establish a connection for a user."""
        raise NotImplementedError

    @abstractmethod
    async def disconnect(self, user_id: str) -> dict[str, Any]:
        """Disconnect an external account for a user."""
        raise NotImplementedError

    @abstractmethod
    async def get_status(self, user_id: str) -> dict[str, Any]:
        """Return the current connection status."""
        raise NotImplementedError

    @abstractmethod
    async def get_capabilities(self) -> list[str]:
        """Return the operations supported by this connector."""
        raise NotImplementedError
