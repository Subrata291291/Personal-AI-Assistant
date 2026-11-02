
from app.integrations.connectors.base import BaseConnector
from app.integrations.connectors.schemas import ConnectorCapability


class ConnectorRegistry:
    """
    Central registry for managing connectors
    and their supported capabilities.
    """

    def __init__(self) -> None:
        self._connectors: dict[str, BaseConnector] = {}
        self._capabilities: dict[
            str, dict[str, ConnectorCapability]
        ] = {}

    def register(
        self,
        connector: BaseConnector,
        capabilities: list[ConnectorCapability] | None = None,
    ) -> None:

        provider = connector.provider.strip().lower()

        if not provider:
            raise ValueError("Connector provider cannot be empty.")

        if provider in self._connectors:
            raise ValueError(
                f"Connector '{provider}' is already registered."
            )

        capability_map: dict[str, ConnectorCapability] = {}

        for capability in capabilities or []:
            capability_name = capability.name.strip().lower()
            capability_provider = capability.provider.strip().lower()

            if capability_provider != provider:
                raise ValueError(
                    f"Capability '{capability_name}' belongs to "
                    f"'{capability_provider}', not '{provider}'."
                )

            if capability_name in capability_map:
                raise ValueError(
                    f"Duplicate capability '{capability_name}'."
                )

            capability_map[capability_name] = capability

        self._connectors[provider] = connector
        self._capabilities[provider] = capability_map

    def get(self, provider: str) -> BaseConnector:
        normalized_provider = provider.strip().lower()

        connector = self._connectors.get(normalized_provider)

        if connector is None:
            raise ValueError(
                f"Connector '{normalized_provider}' is not registered."
            )

        return connector

    def get_capability(
        self,
        provider: str,
        capability_name: str,
    ) -> ConnectorCapability:

        normalized_provider = provider.strip().lower()
        normalized_capability = capability_name.strip().lower()

        capabilities = self._capabilities.get(normalized_provider)

        if capabilities is None:
            raise ValueError(
                f"Connector '{normalized_provider}' is not registered."
            )

        capability = capabilities.get(normalized_capability)

        if capability is None:
            raise ValueError(
                f"Capability '{normalized_capability}' is not registered "
                f"for '{normalized_provider}'."
            )

        return capability

    def list_providers(self) -> list[str]:
        return sorted(self._connectors.keys())

    def list_capabilities(self, provider: str) -> list[str]:
        normalized_provider = provider.strip().lower()

        capabilities = self._capabilities.get(normalized_provider)

        if capabilities is None:
            raise ValueError(
                f"Connector '{normalized_provider}' is not registered."
            )

        return sorted(capabilities.keys())

    def is_registered(self, provider: str) -> bool:
        return provider.strip().lower() in self._connectors
