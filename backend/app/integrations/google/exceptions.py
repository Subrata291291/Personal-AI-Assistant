class GoogleAPIError(Exception):
    def __init__(
        self,
        status_code: int,
        message: str,
        retryable: bool = False,
    ):
        self.status_code = status_code
        self.message = message
        self.retryable = retryable

        super().__init__(
            f"Google API error ({status_code}): {message}"
        )