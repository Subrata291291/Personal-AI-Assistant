import socket
import ssl
import sys
from types import SimpleNamespace

import pytest

from scripts import check_postgres_runtime_role as diagnostic


class WrappedDatabaseError(Exception):
    def __init__(self, *, sqlstate=None, pgcode=None):
        self.orig = DatabaseAPIError(sqlstate=sqlstate, pgcode=pgcode)


class DatabaseAPIError(Exception):
    def __init__(self, *, sqlstate=None, pgcode=None):
        self.sqlstate = sqlstate
        self.pgcode = pgcode


@pytest.mark.parametrize(
    ("exception", "stage", "expected"),
    [
        (socket.gaierror(-2, "private host"), "CONNECTION", "DNS"),
        (TimeoutError("secret detail"), "CONNECTION", "TIMEOUT"),
        (ssl.SSLError("secret detail"), "CONNECTION", "SSL"),
        (
            ConnectionRefusedError("private host"),
            "CONNECTION",
            "CONNECTION_REFUSED",
        ),
        (
            WrappedDatabaseError(sqlstate="28000"),
            "CONNECTION",
            "AUTHENTICATION",
        ),
        (
            WrappedDatabaseError(pgcode="3D000"),
            "CONNECTION",
            "DATABASE_UNAVAILABLE",
        ),
        (
            WrappedDatabaseError(sqlstate="42501"),
            "QUERY_EXECUTION",
            "PERMISSION_DENIED",
        ),
        (
            WrappedDatabaseError(sqlstate="08006"),
            "CONNECTION",
            "CONNECTION_FAILURE",
        ),
        (ValueError("bad configuration"), "ENGINE_INITIALIZATION", "INVALID_CONFIGURATION"),
        (RuntimeError("query failed"), "RESULT_RETRIEVAL", "QUERY_FAILURE"),
    ],
)
def test_error_category_mapping(exception, stage, expected):
    assert diagnostic._error_category(exception, stage) == expected


def test_engine_initialization_failure_is_sanitized(monkeypatch, capsys):
    monkeypatch.setattr(
        diagnostic,
        "_initialize_engine",
        lambda: (_ for _ in ()).throw(ValueError("secret connection string")),
    )
    monkeypatch.setattr(sys, "argv", ["check_postgres_runtime_role.py"])

    assert diagnostic.main() == 1

    output = capsys.readouterr().err
    assert output.splitlines() == [
        "FAILURE_STAGE=ENGINE_INITIALIZATION",
        "EXCEPTION_CLASS=ValueError",
        "ERROR_CATEGORY=INVALID_CONFIGURATION",
    ]
    assert "secret" not in output


def test_connection_failure_reports_dns_and_does_not_leak_message(
    monkeypatch,
    capsys,
):
    engine = SimpleNamespace(
        url=SimpleNamespace(host="localhost"),
        connect=lambda: (_ for _ in ()).throw(socket.gaierror(
            -2,
            "private hostname",
        )),
    )
    monkeypatch.setattr(
        diagnostic,
        "_initialize_engine",
        lambda: (engine, diagnostic.QUERY_SQL),
    )
    monkeypatch.setattr(sys, "argv", ["check_postgres_runtime_role.py"])

    assert diagnostic.main() == 1

    output = capsys.readouterr().err
    assert output.splitlines() == [
        "FAILURE_STAGE=CONNECTION",
        "EXCEPTION_CLASS=gaierror",
        "ERROR_CATEGORY=DNS",
    ]
    assert "private hostname" not in output


def test_query_execution_failure_closes_connection(monkeypatch, capsys):
    class QueryError(Exception):
        sqlstate = "42501"

    class FakeConnection:
        closed = False

        def execute(self, _query):
            raise QueryError("secret query details")

        def close(self):
            self.closed = True

    connection = FakeConnection()
    engine = SimpleNamespace(
        url=SimpleNamespace(host="localhost"),
        connect=lambda: connection,
    )
    monkeypatch.setattr(
        diagnostic,
        "_initialize_engine",
        lambda: (engine, diagnostic.QUERY_SQL),
    )
    monkeypatch.setattr(sys, "argv", ["check_postgres_runtime_role.py"])

    assert diagnostic.main() == 1

    output = capsys.readouterr().err
    assert output.splitlines() == [
        "FAILURE_STAGE=QUERY_EXECUTION",
        "EXCEPTION_CLASS=QueryError",
        "ERROR_CATEGORY=PERMISSION_DENIED",
    ]
    assert "secret" not in output
    assert connection.closed


def test_result_retrieval_failure_reports_stage_and_closes_connection(
    monkeypatch,
    capsys,
):
    class ResultError(Exception):
        pass

    class FakeResult:
        def mappings(self):
            return self

        def one(self):
            raise ResultError("private result detail")

    class FakeConnection:
        closed = False

        def execute(self, _query):
            return FakeResult()

        def close(self):
            self.closed = True

    connection = FakeConnection()
    engine = SimpleNamespace(
        url=SimpleNamespace(host="localhost"),
        connect=lambda: connection,
    )
    monkeypatch.setattr(
        diagnostic,
        "_initialize_engine",
        lambda: (engine, diagnostic.QUERY_SQL),
    )
    monkeypatch.setattr(sys, "argv", ["check_postgres_runtime_role.py"])

    assert diagnostic.main() == 1

    output = capsys.readouterr().err
    assert output.splitlines() == [
        "FAILURE_STAGE=RESULT_RETRIEVAL",
        "EXCEPTION_CLASS=ResultError",
        "ERROR_CATEGORY=QUERY_FAILURE",
    ]
    assert "private result detail" not in output
    assert connection.closed


def test_cleanup_failure_is_reported_without_role_output(monkeypatch, capsys):
    class FakeResult:
        def mappings(self):
            return self

        def one(self):
            return {
                "current_database_role": "runtime",
                "login_database_role": "runtime",
                "is_superuser": False,
                "bypasses_rls": True,
            }

    class FakeConnection:
        def execute(self, _query):
            return FakeResult()

        def close(self):
            raise RuntimeError("secret connection detail")

    engine = SimpleNamespace(
        url=SimpleNamespace(host="localhost"),
        connect=FakeConnection,
    )
    monkeypatch.setattr(
        diagnostic,
        "_initialize_engine",
        lambda: (engine, diagnostic.QUERY_SQL),
    )
    monkeypatch.setattr(sys, "argv", ["check_postgres_runtime_role.py"])

    assert diagnostic.main() == 1

    output = capsys.readouterr()
    assert output.out == ""
    assert output.err.splitlines() == [
        "FAILURE_STAGE=CONNECTION_CLEANUP",
        "EXCEPTION_CLASS=RuntimeError",
        "ERROR_CATEGORY=CONNECTION_FAILURE",
    ]
    assert "secret" not in output.err


def test_remote_host_is_refused_without_allow_remote(monkeypatch, capsys):
    connect_called = False

    def connect():
        nonlocal connect_called
        connect_called = True

    engine = SimpleNamespace(
        url=SimpleNamespace(host="remote.example"),
        connect=connect,
    )
    monkeypatch.setattr(
        diagnostic,
        "_initialize_engine",
        lambda: (engine, diagnostic.QUERY_SQL),
    )
    monkeypatch.setattr(sys, "argv", ["check_postgres_runtime_role.py"])

    assert diagnostic.main() == 2
    assert not connect_called
    assert "FAILURE_STAGE=" not in capsys.readouterr().err
