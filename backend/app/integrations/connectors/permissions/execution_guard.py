
from dataclasses import dataclass

from app.integrations.connectors.permissions.manager import (
    PermissionManager,
)
from app.integrations.connectors.permissions.approval_policy import (
    ApprovalPolicy,
)


@dataclass(frozen=True)
class ExecutionDecision:
    allowed: bool
    reason: str


class ConnectorExecutionGuard:
    """
    Validates connector permissions and approval
    before allowing an action to execute.
    """

    def __init__(self) -> None:
        self.permission_manager = PermissionManager()
        self.approval_policy = ApprovalPolicy()

    def authorize(
        self,
        capability: str,
        granted_permissions: set[str],
        approval_granted: bool = False,
    ) -> ExecutionDecision:

        permission = self.permission_manager.check_permission(
            capability,
            granted_permissions,
        )

        if not permission.allowed:
            return ExecutionDecision(
                allowed=False,
                reason=permission.reason,
            )

        approval = self.approval_policy.check_approval(capability)

        if approval.required and not approval_granted:
            return ExecutionDecision(
                allowed=False,
                reason="Explicit user approval is required.",
            )

        return ExecutionDecision(
            allowed=True,
            reason="Execution authorized.",
        )
