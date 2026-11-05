import json
import logging
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.exc import ProgrammingError

from app.core.request_metrics import (
    RequestMetrics,
    current_request_metrics,
)
from app.repositories.action_approval_repository import (
    ActionApprovalRepository,
    _LOCK_PROPOSAL_IDEMPOTENCY,
)


class FakePostgresError(Exception):
    pgcode = "23505"
    diag = SimpleNamespace(
        message_primary=(
            'duplicate key value violates unique constraint "private_constraint"'
        ),
        message_detail=(
            "Key (body)=(reviewed-body-secret) already exists for "
            "recipient@example.com"
        ),
        message_hint="Check 'private-hint-secret'.",
        constraint_name="private_constraint",
        table_name="private_table",
        column_name="private_column",
    )


class FailingConnection:
    def execute(self, statement, params):
        assert statement is _LOCK_PROPOSAL_IDEMPOTENCY
        assert params["lock_key"]
        original = FakePostgresError("driver error with private data")
        error = ProgrammingError(
            statement=str(statement),
            params={
                **params,
                "to": ["recipient@example.com"],
                "body": "reviewed-body-secret",
            },
            orig=original,
        )
        raise error from original


class FailingTransaction:
    def __enter__(self):
        return FailingConnection()

    def __exit__(self, *_args):
        return False


class FailingEngine:
    def begin(self):
        return FailingTransaction()


def test_database_failure_logs_sanitized_diagnostics_and_full_traceback(
    caplog,
):
    request_id = "request-diagnostics-test"
    idempotency_key = uuid4()
    proposal_id = uuid4()
    token = current_request_metrics.set(RequestMetrics(request_id=request_id))
    caplog.set_level(logging.ERROR)

    try:
        repository = ActionApprovalRepository(FailingEngine())
        with pytest.raises(ProgrammingError):
            repository.create_gmail_draft_proposal(
                proposal_id=proposal_id,
                idempotency_key=idempotency_key,
                source_message_id=uuid4(),
                conversation_id=uuid4(),
                user_id=uuid4(),
                parameters={
                    "to": ["recipient@example.com"],
                    "body": "reviewed-body-secret",
                },
                connection_id=uuid4(),
                account_email="sender@example.com",
                expiration_minutes=15,
            )
    finally:
        current_request_metrics.reset(token)

    message = next(
        record.getMessage()
        for record in caplog.records
        if "gmail_draft_approval_database_transaction_failed" in record.message
    )
    diagnostics = json.loads(message.split("| diagnostics=", 1)[1])

    assert request_id in message
    assert str(idempotency_key) in message
    assert str(proposal_id) in message
    assert diagnostics["exception_class"].endswith(".ProgrammingError")
    assert diagnostics["dbapi_exception_class"].endswith(".FakePostgresError")
    assert diagnostics["sqlstate"] == "23505"
    assert "duplicate key value" in diagnostics["postgres_message"]
    assert diagnostics["postgres_detail"].startswith("Key (body)=([REDACTED])")
    assert diagnostics["postgres_hint"] == "Check [REDACTED]."
    assert diagnostics["constraint"] == "private_constraint"
    assert diagnostics["table"] == "private_table"
    assert diagnostics["column"] == "private_column"
    assert ":lock_key" in diagnostics["sql_statement"]
    assert diagnostics["sql_parameters"] == "[REDACTED]"
    assert "test_action_approval_repository_diagnostics.py" in (
        diagnostics["traceback"]
    )
    assert "line " in diagnostics["traceback"]
    assert "FakePostgresError" in diagnostics["traceback"]
    assert str(proposal_id) not in diagnostics["sql_statement"]
    assert "reviewed-body-secret" not in message
    assert "recipient@example.com" not in message
    assert "sender@example.com" not in message
    assert "driver error with private data" not in message
