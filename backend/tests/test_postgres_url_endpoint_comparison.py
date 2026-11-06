import socket
import sys
from types import SimpleNamespace

import pytest

from scripts import compare_postgres_url_endpoints as diagnostic


def test_endpoint_classification_uses_supabase_host_structure_and_port():
    assert (
        diagnostic._endpoint_class("db.project-ref.supabase.co", 5432)
        == "DIRECT"
    )
    assert (
        diagnostic._endpoint_class(
            "aws-0-region.pooler.supabase.com",
            5432,
        )
        == "SHARED_SESSION_POOLER"
    )
    assert (
        diagnostic._endpoint_class(
            "aws-0-region.pooler.supabase.com",
            6543,
        )
        == "SHARED_TRANSACTION_POOLER"
    )
    assert (
        diagnostic._endpoint_class("db.project-ref.supabase.co", 6543)
        == "OTHER"
    )
    assert diagnostic._endpoint_class("localhost", 5432) == "OTHER"


def test_dns_resolution_reports_success_without_exposing_addresses():
    calls = []

    def resolver(host, port, *, type):
        calls.append((host, port, type))
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 0))]

    assert diagnostic._dns_resolution("private.example", resolver) == "SUCCESS"
    assert calls == [("private.example", None, socket.SOCK_STREAM)]


def test_dns_resolution_reports_failure_without_exposing_error():
    def resolver(_host, _port, *, type):
        raise socket.gaierror(-2, "private hostname")

    assert diagnostic._dns_resolution("private.example", resolver) == "FAILED"


def test_url_inspection_outputs_only_coarse_url_fields(monkeypatch):
    monkeypatch.setattr(
        diagnostic,
        "_dns_resolution",
        lambda _host: "SUCCESS",
    )

    result = diagnostic._inspect_url(
        "postgresql://test-user:test-password@"
        "db.project-ref.supabase.co:5432/test-database"
    )

    assert result == {
        "URL_FORMAT_VALID": "True",
        "SQLALCHEMY_DRIVER": "postgresql+psycopg",
        "PORT": "5432",
        "USERNAME_PRESENT": "True",
        "PASSWORD_PRESENT": "True",
        "DATABASE_PRESENT": "True",
        "ENDPOINT_CLASS": "DIRECT",
        "DNS_RESOLUTION": "SUCCESS",
    }
    assert "test-user" not in repr(result)
    assert "test-password" not in repr(result)
    assert "test-database" not in repr(result)


def test_current_url_comes_from_pydantic_settings_without_session_import(
    monkeypatch,
):
    class FakeSecret:
        def get_secret_value(self):
            return "postgresql://configured"

    config_module = SimpleNamespace(
        settings=SimpleNamespace(DATABASE_URL=FakeSecret())
    )
    monkeypatch.setitem(sys.modules, "app.core.config", config_module)

    assert diagnostic._load_current_configured_url() == (
        "postgresql://configured"
    )


def test_current_url_missing_from_settings_fails_safely(monkeypatch):
    config_module = SimpleNamespace(
        settings=SimpleNamespace(DATABASE_URL=None)
    )
    monkeypatch.setitem(sys.modules, "app.core.config", config_module)

    try:
        diagnostic._load_current_configured_url()
    except RuntimeError as exc:
        assert str(exc) == ""
    else:
        raise AssertionError("Expected missing configured URL to fail")


def test_invalid_url_reports_fixed_safe_values():
    result = diagnostic._inspect_url("not a database URL")

    assert result == {
        "URL_FORMAT_VALID": "False",
        "SQLALCHEMY_DRIVER": "unavailable",
        "PORT": "unavailable",
        "USERNAME_PRESENT": "False",
        "PASSWORD_PRESENT": "False",
        "DATABASE_PRESENT": "False",
        "ENDPOINT_CLASS": "OTHER",
        "DNS_RESOLUTION": "FAILED",
    }


def test_tcp_reachability_closes_socket_on_success():
    class FakeSocket:
        closed = False

        def close(self):
            self.closed = True

    connection = FakeSocket()
    calls = []

    def connector(address, *, timeout):
        calls.append((address, timeout))
        return connection

    assert diagnostic._tcp_reachability(
        "postgresql://test-user:test-password@"
        "db.project-ref.supabase.co:5433/test-database",
        timeout=2.5,
        connector=connector,
    ) == ("SUCCESS", None)
    assert calls == [(("db.project-ref.supabase.co", 5433), 2.5)]
    assert connection.closed


@pytest.mark.parametrize(
    ("exception", "expected_category"),
    [
        (TimeoutError("private detail"), "TIMEOUT"),
        (socket.gaierror(-2, "private host"), "DNS"),
        (ConnectionRefusedError("private host"), "CONNECTION_REFUSED"),
        (OSError("private detail"), "OTHER"),
    ],
)
def test_tcp_reachability_sanitizes_connection_failures(
    exception,
    expected_category,
):
    def connector(_address, *, timeout):
        assert timeout == 5.0
        raise exception

    assert diagnostic._tcp_reachability(
        "postgresql://test-user:test-password@"
        "db.project-ref.supabase.co/test-database",
        connector=connector,
    ) == ("FAILED", expected_category)


def test_tcp_reachability_does_not_connect_for_invalid_url():
    def connector(_address, *, timeout):
        raise AssertionError("connector should not be called")

    assert diagnostic._tcp_reachability(
        "not a URL",
        connector=connector,
    ) == ("FAILED", "OTHER")


def test_tcp_result_output_contains_only_safe_fields(capsys):
    diagnostic._print_tcp_result(("FAILED", "DNS"))

    assert capsys.readouterr().out.splitlines() == [
        "TCP_REACHABILITY=FAILED",
        "ERROR_CATEGORY=DNS",
    ]
