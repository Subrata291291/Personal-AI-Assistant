import argparse
import sys
from pathlib import Path
from typing import Any


BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

ROLE_QUERY = """
SELECT current_user, session_user, r.rolsuper, r.rolbypassrls
FROM pg_roles r
WHERE r.rolname = current_user;
"""

REVISION_QUERY = """
SELECT version_num
FROM public.pa_alembic_version;
"""


def _initialize_preflight():
    from sqlalchemy import text

    from app.db.session import engine

    return engine, text(ROLE_QUERY), text(REVISION_QUERY)


def _read_preflight(
    engine: Any,
    role_query: Any,
    revision_query: Any,
) -> dict[str, Any]:
    connection = engine.connect()
    try:
        role = connection.execute(role_query).mappings().one()
        revision = connection.execute(revision_query).mappings().one()
        return {
            "current_user": role["current_user"],
            "session_user": role["session_user"],
            "rolsuper": role["rolsuper"],
            "rolbypassrls": role["rolbypassrls"],
            "current_revision": revision["version_num"],
        }
    finally:
        connection.close()


def _print_result(result: dict[str, Any] | None) -> None:
    if result is None:
        print("CONNECTION=FAILURE")
        return

    print("CONNECTION=SUCCESS")
    for field in (
        "current_user",
        "session_user",
        "rolsuper",
        "rolbypassrls",
        "current_revision",
    ):
        print(f"{field}={result[field]}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Read the configured PostgreSQL role and Alembic revision using "
            "the application's existing SQLAlchemy engine."
        )
    )
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help=(
            "Allow a non-loopback target after verifying the configured "
            "DATABASE_URL is the intended authorized database."
        ),
    )
    args = parser.parse_args()

    result: dict[str, Any] | None = None
    try:
        engine, role_query, revision_query = _initialize_preflight()
        host = (engine.url.host or "").strip("[]").lower()
        is_loopback = host in {"localhost", "127.0.0.1", "::1"}
        if is_loopback or args.allow_remote:
            result = _read_preflight(engine, role_query, revision_query)
    except Exception:
        result = None

    _print_result(result)
    return 0 if result is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
