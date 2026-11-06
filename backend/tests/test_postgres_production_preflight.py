import sys
from types import SimpleNamespace

from scripts import check_postgres_production_preflight as preflight


class FakeResult:
    def __init__(self, row):
        self.row = row

    def mappings(self):
        return self

    def one(self):
        return self.row


class FakeConnection:
    def __init__(self, role_row, revision_row):
        self.results = [FakeResult(role_row), FakeResult(revision_row)]
        self.queries = []
        self.closed = False

    def execute(self, query):
        self.queries.append(query)
        return self.results.pop(0)

    def close(self):
        self.closed = True


def test_read_preflight_executes_only_role_and_revision_selects():
    role_row = {
        "current_user": "postgres",
        "session_user": "postgres",
        "rolsuper": False,
        "rolbypassrls": True,
    }
    revision_row = {"version_num": "821cd57b946d"}
    connection = FakeConnection(role_row, revision_row)
    engine = SimpleNamespace(connect=lambda: connection)

    result = preflight._read_preflight(
        engine,
        preflight.ROLE_QUERY,
        preflight.REVISION_QUERY,
    )

    assert result == {
        **role_row,
        "current_revision": "821cd57b946d",
    }
    assert connection.queries == [
        preflight.ROLE_QUERY,
        preflight.REVISION_QUERY,
    ]
    assert connection.closed


def test_connection_failure_output_is_sanitized(monkeypatch, capsys):
    engine = SimpleNamespace(
        url=SimpleNamespace(host="remote.example"),
        connect=lambda: (_ for _ in ()).throw(
            RuntimeError("secret URL and hostname")
        ),
    )
    monkeypatch.setattr(
        preflight,
        "_initialize_preflight",
        lambda: (engine, preflight.ROLE_QUERY, preflight.REVISION_QUERY),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["check_postgres_production_preflight.py", "--allow-remote"],
    )

    assert preflight.main() == 1
    output = capsys.readouterr().out
    assert output == "CONNECTION=FAILURE\n"
    assert "secret" not in output


def test_remote_target_requires_explicit_opt_in(monkeypatch, capsys):
    connect_called = False

    def connect():
        nonlocal connect_called
        connect_called = True

    engine = SimpleNamespace(
        url=SimpleNamespace(host="remote.example"),
        connect=connect,
    )
    monkeypatch.setattr(
        preflight,
        "_initialize_preflight",
        lambda: (engine, preflight.ROLE_QUERY, preflight.REVISION_QUERY),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["check_postgres_production_preflight.py"],
    )

    assert preflight.main() == 1
    assert not connect_called
    assert capsys.readouterr().out == "CONNECTION=FAILURE\n"


def test_success_output_contains_only_requested_values(capsys):
    preflight._print_result(
        {
            "current_user": "postgres",
            "session_user": "postgres",
            "rolsuper": False,
            "rolbypassrls": True,
            "current_revision": "821cd57b946d",
        }
    )

    assert capsys.readouterr().out.splitlines() == [
        "CONNECTION=SUCCESS",
        "current_user=postgres",
        "session_user=postgres",
        "rolsuper=False",
        "rolbypassrls=True",
        "current_revision=821cd57b946d",
    ]
