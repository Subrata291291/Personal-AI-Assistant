class GoogleAPIError(Exception):
    def __init__(
        self,
        status_code: int,
        message: str,
        retryable: bool = False,
        reason: str | None = None,
    ):
        self.status_code = status_code
        self.message = message
        self.retryable = retryable
        self.reason = reason

        super().__init__(
            f"Google API error ({status_code}): {message}"
        )


class GoogleOAuthAuthorizationError(GoogleAPIError):
    """A refresh token was definitively rejected by Google's token endpoint."""

    def __init__(self) -> None:
        super().__init__(
            status_code=401,
            message="Google authorization is no longer valid.",
        )


class GoogleOAuthTransientError(GoogleAPIError):
    """A refresh attempt failed for a transient provider or network reason."""

    def __init__(self) -> None:
        super().__init__(
            status_code=429,
            message="Google authentication is temporarily unavailable.",
            retryable=True,
        )


class GoogleAPIMutationError(GoogleAPIError):
    """A mutation request failed, with explicit execution-outcome knowledge."""

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        outcome_unknown: bool,
        reason: str | None = None,
    ) -> None:
        self.outcome_unknown = outcome_unknown
        super().__init__(
            status_code=status_code,
            message=message,
            retryable=False,
            reason=reason,
        )