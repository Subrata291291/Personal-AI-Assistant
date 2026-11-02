
import logging
import sys

from app.core.config import settings


def setup_logging() -> None:
    """
    Configure centralized application logging.
    """

    log_level = (
        logging.DEBUG
        if settings.DEBUG and settings.APP_ENV == "development"
        else logging.INFO
    )

    log_format = (
        "%(asctime)s | "
        "%(levelname)s | "
        "%(name)s | "
        "%(message)s"
    )

    logging.basicConfig(
        level=log_level,
        format=log_format,
        handlers=[
            logging.StreamHandler(sys.stdout)
        ],
        force=True,
    )


def get_logger(name: str) -> logging.Logger:
    """
    Return a named application logger.
    """

    return logging.getLogger(name)
