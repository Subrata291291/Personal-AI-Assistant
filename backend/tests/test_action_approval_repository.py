import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql.psycopg import PGDialect_psycopg

from app.repositories.action_approval_repository import (
    ActionApprovalRepository,
    _GET_PROPOSAL,
    _INSERT_CALENDAR_OPERATION,
    _INSERT_GMAIL_DRAFT_PROPOSAL,
    _INSERT_PROPOSAL,
    _LOCK_PROPOSAL_IDEMPOTENCY,
    _SYNC_CALENDAR_APPROVAL_STATE,
    _TRANSITION,
)


class Result:
    def __init__(self, row=None, rowcount=None):
        self.row = row
        self.rowcount = rowcount

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


def test_generic_insert_types_action_type_without_changing_bind_order():
    compiled = _INSERT_PROPOSAL.compile(
        dialect=postgresql.dialect(paramstyle="numeric"),
    )

    assert compiled.positiontup[4] == "action_type"
    assert " 'action_type', CAST(:5 AS text)" in compiled.string

    psycopg_compiled = _INSERT_PROPOSAL.compile(
        dialect=PGDialect_psycopg(),
    )
    assert (
        "'action_type', CAST(%(action_type)s AS text)"
        in psycopg_compiled.string
    )


@pytest.mark.parametrize(
    "timestamp_field",
    [None, "approved_at"],
    ids=["null-timestamp-field", "populated-timestamp-field"],
)
def test_transition_explicitly_types_nullable_timestamp_field(
    timestamp_field,
):
    compiled = _TRANSITION.compile(
        dialect=postgresql.dialect(paramstyle="numeric"),
    )

    assert compiled.positiontup[1] == "timestamp_field"
    assert (
        "WHEN CAST(:2 AS text) IS NULL THEN '{}'::jsonb"
        in compiled.string
    )

    connection = TransitionConnection()
    repository = ActionApprovalRepository(ApprovalInsertEngine(connection))
    result = repository.transition(
        user_id=uuid4(),
        proposal_id=uuid4(),
        expected_status="pending",
        new_status="approved",
        timestamp_field=timestamp_field,
    )

    assert result is None
    assert connection.statement is _TRANSITION
    assert connection.parameters["timestamp_field"] == timestamp_field


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


def test_generic_proposal_creation_uses_typed_action_type_bind():
    connection = GenericApprovalInsertConnection()
    repository = ActionApprovalRepository(ApprovalInsertEngine(connection))

    result = repository.create_proposal(
        proposal_id=uuid4(),
        user_id=uuid4(),
        conversation_id=uuid4(),
        source_message_id=uuid4(),
        action_type="gmail.send",
        parameters={"to": ["recipient@example.com"]},
        connection_id=uuid4(),
        account_email="sender@example.com",
        expiration_minutes=15,
    )

    assert result is None
    assert connection.statement is _INSERT_PROPOSAL
    assert connection.parameters["action_type"] == "gmail.send"


def test_calendar_replacement_rejects_old_pending_and_inserts_new_atomically():
    user_id = uuid4()
    conversation_id = uuid4()
    source_message_id = uuid4()
    connection_id = uuid4()
    old_proposal_id = uuid4()
    new_proposal_id = uuid4()
    now = datetime.now(timezone.utc)
    old_proposal = {
        "proposal_id": str(old_proposal_id),
        "user_id": str(user_id),
        "conversation_id": str(conversation_id),
        "connection_id": str(connection_id),
        "action_type": "calendar.create_event",
        "parameters": {"summary": "Gym"},
        "status": "pending",
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=10)).isoformat(),
    }

    class ReplacementConnection:
        def __init__(self):
            self.statements = []
            self.transition_parameters = None

        def execute(self, statement, parameters):
            self.statements.append(statement)
            if statement is _LOCK_PROPOSAL_IDEMPOTENCY:
                return Result()
            if statement is _GET_PROPOSAL:
                if parameters["proposal_id"] == str(new_proposal_id):
                    return Result()
                return Result({
                    "message_id": str(old_proposal_id),
                    "proposal": old_proposal,
                    "account_email": "user@example.com",
                })
            if statement is _TRANSITION:
                self.transition_parameters = parameters
                return Result({"proposal": old_proposal})
            if statement is _SYNC_CALENDAR_APPROVAL_STATE:
                return Result(rowcount=1)
            if statement is _INSERT_PROPOSAL:
                proposal = {
                    "proposal_id": str(new_proposal_id),
                    "user_id": str(user_id),
                    "conversation_id": str(conversation_id),
                    "source_message_id": str(source_message_id),
                    "connection_id": str(connection_id),
                    "action_type": "calendar.create_event",
                    "parameters": {"summary": "Gym"},
                    "status": "pending",
                    "created_at": now.isoformat(),
                    "expires_at": (now + timedelta(minutes=15)).isoformat(),
                    "operation_id": "e840d4d7-c47f-45ad-805d-b76a76372a68",
                    "payload_fingerprint": "new-fingerprint",
                }
                return Result({
                    "message_id": str(new_proposal_id),
                    "proposal": proposal,
                    "account_email": "user@example.com",
                })
            assert statement is _INSERT_CALENDAR_OPERATION
            return Result()

    connection = ReplacementConnection()
    repository = ActionApprovalRepository(
        ApprovalInsertEngine(connection),
    )
    result = repository.create_proposal(
        proposal_id=new_proposal_id,
        user_id=user_id,
        conversation_id=conversation_id,
        source_message_id=source_message_id,
        action_type="calendar.create_event",
        parameters={"summary": "Gym"},
        connection_id=connection_id,
        account_email="user@example.com",
        expiration_minutes=15,
        calendar_operation={
            "proposal_id": str(new_proposal_id),
            "operation_id": "e840d4d7-c47f-45ad-805d-b76a76372a68",
            "user_id": str(user_id),
            "connection_id": str(connection_id),
            "payload_fingerprint": "new-fingerprint",
            "action_type": "calendar.create_event",
            "calendar_id": "primary",
            "event_id": None,
            "integrity_schema_version": 1,
        },
        supersedes_proposal_id=old_proposal_id,
    )

    assert result["proposal_id"] == new_proposal_id
    assert result["status"] == "pending"
    assert connection.statements.index(_TRANSITION) < (
        connection.statements.index(_INSERT_PROPOSAL)
    )
    assert _SYNC_CALENDAR_APPROVAL_STATE in connection.statements
    assert json.loads(connection.transition_parameters["patch"]) == {
        "result": {"outcome": "superseded"},
        "status": "rejected",
    }


class TransitionConnection:
    def __init__(self):
        self.statement = None
        self.parameters = None

    def execute(self, statement, parameters):
        self.statement = statement
        self.parameters = parameters
        return Result()


class GenericApprovalInsertConnection:
    def __init__(self):
        self.statement = None
        self.parameters = None

    def execute(self, statement, parameters):
        self.statement = statement
        self.parameters = parameters
        return Result()
