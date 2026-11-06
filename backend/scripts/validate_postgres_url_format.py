import os
import sys

from sqlalchemy.engine import URL, make_url


SUPPORTED_SCHEMES = {
    "postgres": "postgresql+psycopg",
    "postgresql": "postgresql+psycopg",
    "postgresql+psycopg": "postgresql+psycopg",
}


def _print_result(
    *,
    valid: bool,
    driver: str,
    username_present: bool,
    password_present: bool,
    database_present: bool,
    error_type: str | None = None,
) -> None:
    print(f"URL_FORMAT_VALID={valid}")
    print(f"SQLALCHEMY_DRIVER={driver}")
    print(f"USERNAME_PRESENT={username_present}")
    print(f"PASSWORD_PRESENT={password_present}")
    print(f"DATABASE_PRESENT={database_present}")
    if error_type is not None:
        print(f"ERROR_TYPE={error_type}")


def main() -> int:
    raw_url = os.environ.get("DATABASE_URL")
    if not raw_url:
        _print_result(
            valid=False,
            driver="unavailable",
            username_present=False,
            password_present=False,
            database_present=False,
            error_type="ValueError",
        )
        return 1

    url: URL | None = None
    try:
        url = make_url(raw_url)
        normalized_driver = SUPPORTED_SCHEMES.get(url.drivername)
        if normalized_driver is None:
            raise ValueError("Unsupported database URL scheme.")
        if url.drivername != normalized_driver:
            url = url.set(drivername=normalized_driver)
        if "sslmode" not in url.query:
            url = url.update_query_dict({"sslmode": "require"})
    except Exception as exc:
        _print_result(
            valid=False,
            driver="unavailable",
            username_present=bool(url and url.username),
            password_present=bool(url and url.password),
            database_present=bool(url and url.database),
            error_type=type(exc).__name__,
        )
        return 1

    _print_result(
        valid=True,
        driver=url.drivername,
        username_present=bool(url.username),
        password_present=bool(url.password),
        database_present=bool(url.database),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
