from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from app.repositories.action_approval_repository import (
    ActionApprovalRepository,
    _GET_PROPOSAL,
    _INSERT_GMAIL_DRAFT_PROPOSAL,
    _INSERT_PROPOSAL,
    _LOCK_PROPOSAL_IDEMPOTENCY,
)


class Result:
    def __init__(self, row=None):
        self.row = row

    def mappings(self):
        return self

    def first(self):
        return self.row


class ApprovalInsertConnection:
    def __init__(self, account_email):
        self.account_email = account_email
        self.insert_statement = None
        self.insert_parameters = None

    def execute(self, statement, parameters):
        if statement is _LOCK_PROPOSAL_IDEMPOTENCY:
            return Result()
        if statement is _GET_PROPOSAL:
            return Result()
        assert statement is _INSERT_GMAIL_DRAFT_PROPOSAL
        self.insert_statement = statement
        self.insert_parameters = parameters
        proposal_id = parameters["proposal_id"]
        now = datetime.now(timezone.utc)
        return Result({
            "message_id": proposal_id,
            "metadata": {
                "action_approval": {
                    "proposal_id": proposal_id,
                    "source_message_id": parameters["source_message_id"],
                    "user_id": parameters["user_id"],
                    "conversation_id": parameters["conversation_id"],
                    "connection_id": parameters["connection_id"],
                    "action_type": "gmail.send_draft",
                    "parameters": {},
                    "status": "pending",
                    "created_at": now.isoformat(),
                    "expires_at": now.isoformat(),
                    "account_email": self.account_email,
                },
            },
        })


class ApprovalInsertTransaction:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, *_args):
        return False


class ApprovalInsertEngine:
    def __init__(self, connection):
        self.connection = connection

    def begin(self):
        return ApprovalInsertTransaction(self.connection)


def _insert_proposal(account_email):
    connection = ApprovalInsertConnection(account_email)
    repository = ActionApprovalRepository(ApprovalInsertEngine(connection))
    result = repository.create_gmail_draft_proposal(
        proposal_id=uuid4(),
        idempotency_key=uuid4(),
        source_message_id=uuid4(),
        conversation_id=uuid4(),
        user_id=uuid4(),
        parameters={"draft_id": "draft-id"},
        connection_id=uuid4(),
        account_email=account_email,
        expiration_minutes=15,
    )
    return result, connection


def test_postgres_42p18_regression_types_seventh_bind_as_text():
    compiled = _INSERT_GMAIL_DRAFT_PROPOSAL.compile(
        dialect=postgresql.dialect(paramstyle="numeric"),
    )

    assert compiled.positiontup[6] == "account_email"
    assert " 'account_email', CAST(:7 AS text)" in compiled.string
    assert "make_interval(mins => :8)" in compiled.string
    assert "'account_email', CAST(:account_email AS text)" in str(
        _INSERT_PROPOSAL
    )


@pytest.mark.parametrize(
    "account_email",
    ["sender@example.com", None],
    ids=["populated-account-email", "null-account-email"],
)
def test_gmail_draft_proposal_insert_accepts_nullable_account_email(
    account_email,
):
    proposal, connection = _insert_proposal(account_email)

    assert proposal["account_email"] == account_email
    assert connection.insert_parameters["account_email"] == account_email
    assert "CAST(:account_email AS text)" in str(connection.insert_statement)
