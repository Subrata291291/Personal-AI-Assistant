
from dataclasses import dataclass


@dataclass(frozen=True)
class PermissionDecision:
    allowed: bool
    reason: str


class PermissionManager:
    """
    Validates whether a user is allowed to execute
    a connector capability.
    """

    def check_permission(
        self,
        capability: str,
        granted_permissions: set[str],
    ) -> PermissionDecision:

        normalized_capability = capability.strip().lower()

        if not normalized_capability:
            return PermissionDecision(
                allowed=False,
                reason="Capability cannot be empty.",
            )

        normalized_permissions = {
            permission.strip().lower()
            for permission in granted_permissions
        }

        if normalized_capability not in normalized_permissions:
            return PermissionDecision(
                allowed=False,
                reason=f"Permission not granted: {normalized_capability}",
            )

        return PermissionDecision(
            allowed=True,
            reason="Permission granted.",
        )
