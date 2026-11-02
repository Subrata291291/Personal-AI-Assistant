
from dataclasses import dataclass


@dataclass(frozen=True)
class ApprovalDecision:
    required: bool
    reason: str


class ApprovalPolicy:
    """
    Determines whether a connector action
    requires explicit user approval.
    """

    APPROVAL_REQUIRED_ACTIONS = {
        "send_email",
        "delete_email",
        "create_calendar_event",
        "delete_calendar_event",
    }

    def check_approval(
        self,
        capability: str,
    ) -> ApprovalDecision:

        normalized_capability = capability.strip().lower()

        if not normalized_capability:
            raise ValueError("Capability cannot be empty.")

        if normalized_capability in self.APPROVAL_REQUIRED_ACTIONS:
            return ApprovalDecision(
                required=True,
                reason="This action requires explicit user approval.",
            )

        return ApprovalDecision(
            required=False,
            reason="No explicit approval required by the current policy.",
        )
