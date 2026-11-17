import pytest
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID, uuid4
from datetime import datetime, timezone

from app.repositories.action_approval_repository import ActionApprovalRepository
from app.services.action_approval_service import ActionApprovalService


USER_ID = uuid4()
CONNECTION_ID = uuid4()
PROPOSAL_ID = uuid4()
SOURCE_MESSAGE_ID = uuid4()
CONVERSATION_ID = uuid4()


def _contacts_integrity():
    return {
        "integrity_schema_version": 1,
        "payload_fingerprint": "f" * 64,
        "operation_id": str(uuid4()),
        "event_id": None,
    }


def _db_row(proposal_id, user_id, source_message_id, conversation_id, connection_id):
    return {
        "message_id": proposal_id,
        "metadata": {
            "action_approval": {
                "proposal_id": str(proposal_id),
                "source_message_id": str(source_message_id),
                "user_id": str(user_id),
                "conversation_id": str(conversation_id),
                "action_type": "contacts.create_contact",
                "parameters": {"display_name": "Ada", "email": "ada@example.com"},
                "connection_id": str(connection_id),
                "account_email": "owner@example.com",
                "created_at": "2026-01-01T00:00:00+00:00",
                "expires_at": "2026-01-01T00:30:00+00:00",
                "status": "pending",
                "approved_at": None,
                "execution_started_at": None,
                "completed_at": None,
                "result": None,
                "integrity_schema_version": 1,
                "payload_fingerprint": "f" * 64,
                "operation_id": str(uuid4()),
                "calendar_event_id": None,
            }
        },
        "created_at": "2026-01-01T00:00:00+00:00",
        "account_email": "owner@example.com",
    }


def _normalized_row(proposal_id, user_id, source_message_id, conversation_id, connection_id):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return {
        "proposal_id": proposal_id,
        "message_id": proposal_id,
        "source_message_id": source_message_id,
        "user_id": user_id,
        "conversation_id": conversation_id,
        "action_type": "contacts.create_contact",
        "parameters": {"display_name": "Ada", "email": "ada@example.com"},
        "connection_id": connection_id,
        "account_email": "owner@example.com",
        "created_at": now,
        "expires_at": datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc),
        "status": "pending",
        "approved_at": None,
        "execution_started_at": None,
        "completed_at": None,
        "result": None,
        "execution_available": False,
        "integrity_schema_version": 1,
        "payload_fingerprint": "f" * 64,
        "operation_id": uuid4(),
        "calendar_event_id": None,
    }


def test_repo_create_contacts_proposal_uses_external_connection_without_begin():
    external = Mock(name="external_connection")
    execute_result = Mock()
    execute_result.mappings.return_value.first.return_value = _db_row(
        PROPOSAL_ID, USER_ID, SOURCE_MESSAGE_ID, CONVERSATION_ID, CONNECTION_ID
    )
    external.execute.return_value = execute_result

    engine = Mock(name="engine")
    begin_cm = Mock(name="begin_cm")
    engine.begin.return_value = begin_cm

    repo = ActionApprovalRepository(database_engine=engine)
    row = repo.create_standalone_contacts_proposal(
        proposal_id=PROPOSAL_ID,
        source_message_id=SOURCE_MESSAGE_ID,
        conversation_id=CONVERSATION_ID,
        user_id=USER_ID,
        action_type="contacts.create_contact",
        parameters={"display_name": "Ada", "email": "ada@example.com"},
        connection_id=CONNECTION_ID,
        account_email="owner@example.com",
        expiration_minutes=30,
        contacts_integrity=_contacts_integrity(),
        connection=external,
    )

    assert row["proposal_id"] == PROPOSAL_ID
    external.execute.assert_called_once()
    engine.begin.assert_not_called()
    external.commit.assert_not_called()
    external.rollback.assert_not_called()
    external.close.assert_not_called()


def test_repo_create_contacts_proposal_external_failure_propagates_without_commit_rollback_close():
    external = Mock(name="external_connection")
    external.execute.side_effect = RuntimeError("insert failed")

    repo = ActionApprovalRepository(database_engine=Mock(name="engine"))
    with pytest.raises(RuntimeError, match="insert failed"):
        repo.create_standalone_contacts_proposal(
            proposal_id=PROPOSAL_ID,
            source_message_id=SOURCE_MESSAGE_ID,
            conversation_id=CONVERSATION_ID,
            user_id=USER_ID,
            action_type="contacts.create_contact",
            parameters={"display_name": "Ada", "email": "ada@example.com"},
            connection_id=CONNECTION_ID,
            account_email="owner@example.com",
            expiration_minutes=30,
            contacts_integrity=_contacts_integrity(),
            connection=external,
        )

    external.commit.assert_not_called()
    external.rollback.assert_not_called()
    external.close.assert_not_called()


def test_repo_create_contacts_proposal_legacy_path_owns_transaction():
    managed = Mock(name="managed_connection")
    execute_result = Mock()
    execute_result.mappings.return_value.first.return_value = _db_row(
        PROPOSAL_ID, USER_ID, SOURCE_MESSAGE_ID, CONVERSATION_ID, CONNECTION_ID
    )
    managed.execute.return_value = execute_result

    begin_cm = Mock()
    begin_cm.__enter__ = Mock(return_value=managed)
    begin_cm.__exit__ = Mock(return_value=False)

    engine = Mock(name="engine")
    engine.begin.return_value = begin_cm

    repo = ActionApprovalRepository(database_engine=engine)
    row = repo.create_standalone_contacts_proposal(
        proposal_id=PROPOSAL_ID,
        source_message_id=SOURCE_MESSAGE_ID,
        conversation_id=CONVERSATION_ID,
        user_id=USER_ID,
        action_type="contacts.create_contact",
        parameters={"display_name": "Ada", "email": "ada@example.com"},
        connection_id=CONNECTION_ID,
        account_email="owner@example.com",
        expiration_minutes=30,
        contacts_integrity=_contacts_integrity(),
    )

    assert row["proposal_id"] == PROPOSAL_ID
    engine.begin.assert_called_once()
    begin_cm.__enter__.assert_called_once()
    begin_cm.__exit__.assert_called_once()
    managed.execute.assert_called_once()


def test_service_create_contacts_proposal_passes_external_connection_and_preserves_ids(monkeypatch):
    connection_obj = SimpleNamespace(id=CONNECTION_ID, email="owner@example.com")
    captured = {}

    class Repo:
        def create_standalone_contacts_proposal(self, **kwargs):
            captured.update(kwargs)
            return _normalized_row(
                kwargs["proposal_id"],
                kwargs["user_id"],
                kwargs["source_message_id"],
                kwargs["conversation_id"],
                kwargs["connection_id"],
            )

    service = ActionApprovalService.__new__(ActionApprovalService)
    service.PROPOSAL_TTL_MINUTES = 30
    service.repository = Repo()
    monkeypatch.setattr(
        ActionApprovalService,
        "_validate_connection",
        lambda self, **_kwargs: connection_obj,
    )

    external = object()
    proposal = ActionApprovalService.create_standalone_contacts_proposal(
        service,
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        action_type="contacts.create_contact",
        parameters={"display_name": "Ada", "email": "ada@example.com"},
        proposal_id=PROPOSAL_ID,
        source_message_id=SOURCE_MESSAGE_ID,
        conversation_id=CONVERSATION_ID,
        db_connection=external,
    )

    assert proposal.proposal_id == PROPOSAL_ID
    assert proposal.source_message_id == SOURCE_MESSAGE_ID
    assert proposal.conversation_id == CONVERSATION_ID
    assert captured["connection"] is external
    assert captured["proposal_id"] == PROPOSAL_ID
    assert captured["source_message_id"] == SOURCE_MESSAGE_ID
    assert captured["conversation_id"] == CONVERSATION_ID


def test_service_create_contacts_proposal_legacy_call_still_creates_valid_proposal(monkeypatch):
    connection_obj = SimpleNamespace(id=CONNECTION_ID, email="owner@example.com")
    captured = {}

    class Repo:
        def create_standalone_contacts_proposal(self, **kwargs):
            captured.update(kwargs)
            return _normalized_row(
                kwargs["proposal_id"],
                kwargs["user_id"],
                kwargs["source_message_id"],
                kwargs["conversation_id"],
                kwargs["connection_id"],
            )

    service = ActionApprovalService.__new__(ActionApprovalService)
    service.PROPOSAL_TTL_MINUTES = 30
    service.repository = Repo()
    monkeypatch.setattr(
        ActionApprovalService,
        "_validate_connection",
        lambda self, **_kwargs: connection_obj,
    )

    proposal = ActionApprovalService.create_standalone_contacts_proposal(
        service,
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        action_type="contacts.create_contact",
        parameters={"display_name": "Ada", "email": "ada@example.com"},
    )

    assert isinstance(proposal.proposal_id, UUID)
    assert isinstance(proposal.source_message_id, UUID)
    assert isinstance(proposal.conversation_id, UUID)
    assert captured["connection"] is None
    assert captured["account_email"] == "owner@example.com"
    assert captured["contacts_integrity"]["payload_fingerprint"]
    assert captured["contacts_integrity"]["operation_id"]
