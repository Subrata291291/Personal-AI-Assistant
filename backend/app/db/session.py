
from collections.abc import Generator
from time import perf_counter

from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.core.request_metrics import record_timing


def build_database_url():
    """
    Build a SQLAlchemy-compatible PostgreSQL URL.

    Uses psycopg 3 as the PostgreSQL driver.
    """

    if not settings.DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured.")

    raw_url = settings.DATABASE_URL.get_secret_value()
    url = make_url(raw_url)

    # Explicitly use psycopg 3.
    if url.drivername in ("postgresql", "postgres"):
        url = url.set(drivername="postgresql+psycopg")

    # Require encrypted database connections.
    if "sslmode" not in url.query:
        url = url.update_query_dict({"sslmode": "require"})

    return url


# ==========================================
# DATABASE ENGINE
# ==========================================

engine = create_engine(
    build_database_url(),
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=5,
    pool_recycle=1800,
    echo=False,
)


@event.listens_for(engine, "before_cursor_execute")
def _start_query_timer(
    _conn,
    _cursor,
    _statement,
    _parameters,
    context,
    _executemany,
):
    context._assistant_query_started_at = perf_counter()


@event.listens_for(engine, "after_cursor_execute")
def _record_query_duration(
    _conn,
    _cursor,
    _statement,
    _parameters,
    context,
    _executemany,
):
    started_at = getattr(context, "_assistant_query_started_at", None)
    if started_at is not None:
        record_timing("database", (perf_counter() - started_at) * 1000)


# ==========================================
# DATABASE SESSION FACTORY
# ==========================================

SessionLocal = sessionmaker(
    bind=engine,
    class_=Session,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
)


# ==========================================
# FASTAPI DATABASE DEPENDENCY
# ==========================================

def get_db() -> Generator[Session, None, None]:
    """
    Provide a database session to a FastAPI request.

    The session is always closed after the request.
    """

    db = SessionLocal()

    try:
        yield db
    finally:
        db.close()
