import logging
import traceback


def log_exception_traceback(
    logger: logging.Logger,
    message: str,
    error: BaseException,
) -> None:
    """Log exception types and stack frames without exception messages."""
    diagnostics = []
    seen: set[int] = set()
    current: BaseException | None = error

    while current is not None and id(current) not in seen:
        seen.add(id(current))
        diagnostics.append(f"{type(current).__name__} traceback:\n")
        if current.__traceback__ is not None:
            diagnostics.extend(
                traceback.format_list(
                    traceback.extract_tb(current.__traceback__)
                )
            )
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )

    logger.error("%s\n%s", message, "".join(diagnostics))
