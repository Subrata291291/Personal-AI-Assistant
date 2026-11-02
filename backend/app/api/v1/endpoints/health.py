
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.db.session import get_db


router = APIRouter()
logger = get_logger(__name__)


@router.get("/health")
def health_check(db: Session = Depends(get_db)):
    """
    Check application and database availability.
    """

    try:
        db.execute(text("SELECT 1"))

    except SQLAlchemyError as exc:
        logger.error(
            "Database health check failed: %s",
            type(exc).__name__,
        )

        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "status": "unhealthy",
                "database": "disconnected",
            },
        )

    return {
        "status": "healthy",
        "application": settings.APP_NAME,
        "database": "connected",
        "version": "1.0.0",
    }
