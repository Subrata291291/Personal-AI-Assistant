import argparse
import errno
import socket
import ssl
import sys
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

QUERY_SQL = """
SELECT
    current_user AS current_database_role,
    session_user AS login_database_role,
    rol.rolsuper AS is_superuser,
    rol.rolbypassrls AS bypasses_rls
FROM pg_roles rol
WHERE rol.rolname = current_user
"""

RESULT_FIELDS = (
    "current_database_role",
    "login_database_role",
    "is_superuser",
    "bypasses_rls",
)


def _exception_chain(exception: BaseException):
    pending = [exception]
    seen: set[int] = set()
    while pending:
        current = pending.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        for nested in (
            getattr(current, "orig", None),
            getattr(current, "__cause__", None),
            getattr(current, "__context__", None),
        ):
            if isinstance(nested, BaseException) and id(nested) not in seen:
                pending.append(nested)


def _error_category(
    exception: BaseException,
    failure_stage: str,
) -> str:
    exceptions = tuple(_exception_chain(exception))
    for current in exceptions:
        if isinstance(current, socket.gaierror):
            return "DNS"
        if isinstance(current, (TimeoutError, socket.timeout)):
            return "TIMEOUT"
        if isinstance(current, ssl.SSLError):
            return "SSL"
        if isinstance(current, ConnectionRefusedError):
            return "CONNECTION_REFUSED"

    for current in exceptions:
        error_number = getattr(current, "errno", None)
        if error_number == errno.ETIMEDOUT:
            return "TIMEOUT"
        if error_number == errno.ECONNREFUSED:
            return "CONNECTION_REFUSED"

    for current in exceptions:
        sqlstate = (
            getattr(current, "sqlstate", None)
            or getattr(current, "pgcode", None)
        )
        if isinstance(sqlstate, str):
            if sqlstate == "3D000":
                return "DATABASE_UNAVAILABLE"
            if sqlstate == "42501":
                return "PERMISSION_DENIED"
            if sqlstate.startswith("28"):
                return "AUTHENTICATION"
            if sqlstate.startswith("08"):
                return "CONNECTION_FAILURE"

    if failure_stage == "ENGINE_INITIALIZATION":
        return "INVALID_CONFIGURATION"
    if failure_stage in {"CONNECTION", "CONNECTION_CLEANUP"}:
        return "CONNECTION_FAILURE"
    return "QUERY_FAILURE"


def _report_failure(
    failure_stage: str,
    exception: BaseException,
) -> None:
    print(f"FAILURE_STAGE={failure_stage}", file=sys.stderr)
    print(
        f"EXCEPTION_CLASS={type(exception).__name__}",
        file=sys.stderr,
    )
    print(
        f"ERROR_CATEGORY={_error_category(exception, failure_stage)}",
        file=sys.stderr,
    )


def _initialize_engine():
    from sqlalchemy import text

    from app.db.session import engine

    return engine, text(QUERY_SQL)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Print PostgreSQL role attributes for one new connection using "
            "the application's configured SQLAlchemy engine."
        )
    )
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help=(
            "Allow a non-loopback database target. Use only after verifying "
            "the configured DATABASE_URL is the intended authorized target."
        ),
    )
    args = parser.parse_args()

    try:
        engine, query = _initialize_engine()
        host = (engine.url.host or "").strip("[]").lower()
    except Exception as exc:
        _report_failure("ENGINE_INITIALIZATION", exc)
        return 1

    is_loopback = host in {"localhost", "127.0.0.1", "::1"}
    if not is_loopback and not args.allow_remote:
        print(
            "Refusing a non-loopback database target. Verify the configured "
            "target, then explicitly pass --allow-remote if authorized.",
            file=sys.stderr,
        )
        return 2

    connection = None
    row: Any = None
    failed = False
    try:
        connection = engine.connect()
    except Exception as exc:
        _report_failure("CONNECTION", exc)
        return 1

    try:
        try:
            result = connection.execute(query)
        except Exception as exc:
            _report_failure("QUERY_EXECUTION", exc)
            failed = True

        if not failed:
            try:
                row = result.mappings().one()
            except Exception as exc:
                _report_failure("RESULT_RETRIEVAL", exc)
                failed = True
    finally:
        try:
            connection.close()
        except Exception as exc:
            _report_failure("CONNECTION_CLEANUP", exc)
            failed = True

    if failed:
        return 1

    for field in RESULT_FIELDS:
        print(f"{field}={row[field]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
