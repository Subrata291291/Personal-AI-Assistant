from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api import deps
from app.api.v1.endpoints.action_approvals import get_action_approval_service
from app.api.v1.endpoints import action_approvals as action_approvals_endpoint
from app.db.session import get_db
from app.main import app
from app.schemas.action_approval import (
    ActionApprovalProposal,
    ApprovalProposalCreate,
)
from app.services.action_approval_service import ActionApprovalService
from app.services.chat_service import ChatService
from app.services.calendar_action_identity import (
    build_calendar_operation_identity,
)


USER_ID = uuid4()
VALID_REQUEST = {
    "conversation_id": "485bc3f9-2b55-4022-a5b4-619f2be167c8",
    "source_message_id": "535bc3f9-2b55-4022-a5b4-619f2be167c8",
    "connection_id": "635bc3f9-2b55-4022-a5b4-619f2be167c8",
    "action_type": "gmail.send",
    "parameters": {
        "to": ["recipient@example.com"],
        "subject": "Reviewed subject",
        "body": "Reviewed message",
    },
}


def _proposal(user_id, payload):
    now = datetime.now(timezone.utc)
    return ActionApprovalProposal(
        proposal_id=uuid4(),
        message_id=uuid4(),
        source_message_id=payload.source_message_id,
        user_id=user_id,
        conversation_id=payload.conversation_id,
        action_type=payload.action_type,
        parameters=payload.parameters,
        connection_id=payload.connection_id,
        account_email="sender@example.com",
        created_at=now,
        expires_at=now + timedelta(minutes=15),
        status="pending",
    )


def test_approval_proposal_create_parses_json_uuid_strings():
    payload = ApprovalProposalCreate.model_validate(VALID_REQUEST)

    assert isinstance(payload.conversation_id, UUID)
    assert isinstance(payload.source_message_id, UUID)
    assert isinstance(payload.connection_id, UUID)
    assert payload.model_config["strict"] is True


@pytest.mark.parametrize(
    "field",
    ["conversation_id", "source_message_id", "connection_id"],
)
def test_approval_proposal_create_rejects_malformed_uuid_strings(field):
    invalid_request = {**VALID_REQUEST, field: "not-a-uuid"}

    with pytest.raises(ValidationError):
        ApprovalProposalCreate.model_validate(invalid_request)


def test_approval_proposal_create_keeps_unrelated_fields_strict():
    invalid_request = {**VALID_REQUEST, "parameters": "not-a-dictionary"}

    with pytest.raises(ValidationError):
        ApprovalProposalCreate.model_validate(invalid_request)


@pytest.mark.parametrize(
    "field",
    ["conversation_id", "source_message_id", "connection_id"],
)
def test_invalid_approval_uuid_returns_422_before_service_call(
    field,
):
    class ServiceStub:
        calls = []

        def create_proposal(self, **kwargs):
            self.calls.append(kwargs)
            return _proposal(kwargs["user_id"], kwargs["payload"])

    service = ServiceStub()
    app.dependency_overrides[deps.get_current_user] = lambda: {
        "id": str(USER_ID),
    }
    app.dependency_overrides[get_action_approval_service] = lambda: service
    app.dependency_overrides[get_db] = lambda: object()

    try:
        response = TestClient(app).post(
            "/api/v1/action-approvals",
            json={**VALID_REQUEST, field: "not-a-uuid"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
    assert service.calls == []


def test_approval_endpoint_passes_parsed_uuids_to_service():
    class ServiceStub:
        calls = []

        def create_proposal(self, *, user_id, payload):
            self.calls.append((user_id, payload))
            return _proposal(user_id, payload)

    service = ServiceStub()
    app.dependency_overrides[deps.get_current_user] = lambda: {
        "id": str(USER_ID),
    }
    app.dependency_overrides[get_action_approval_service] = lambda: service
    app.dependency_overrides[get_db] = lambda: object()

    try:
        response = TestClient(app).post(
            "/api/v1/action-approvals",
            json=VALID_REQUEST,
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 201
    service_user_id, payload = service.calls[0]
    assert service_user_id == USER_ID
    assert isinstance(payload.conversation_id, UUID)
    assert isinstance(payload.source_message_id, UUID)
    assert isinstance(payload.connection_id, UUID)
    assert response.json()["proposal"]["connection_id"] == str(
        payload.connection_id,
    )


def test_create_proposal_preserves_ownership_checks():
    service = object.__new__(ActionApprovalService)
    payload = ApprovalProposalCreate.model_validate(VALID_REQUEST)
    user_id = uuid4()
    ownership_checks = []
    repository_calls = []

    def validate_connection(*, user_id, connection_id, action_type):
        ownership_checks.append((user_id, connection_id, action_type))
        return SimpleNamespace(email="sender@example.com")

    class RepositoryStub:
        def create_proposal(self, **kwargs):
            repository_calls.append(kwargs)
            return None

    service._validate_connection = validate_connection
    service.repository = RepositoryStub()

    with pytest.raises(LookupError, match="conversation or source"):
        service.create_proposal(user_id=user_id, payload=payload)

    assert ownership_checks == [(
        user_id,
        payload.connection_id,
        payload.action_type,
    )]
    assert repository_calls[0]["user_id"] == user_id
    assert repository_calls[0]["conversation_id"] == payload.conversation_id
    assert repository_calls[0]["source_message_id"] == payload.source_message_id


def test_chat_service_exposes_only_current_users_pending_calendar_proposal():
    user_id = uuid4()
    conversation_id = uuid4()
    proposal = {
        "proposal_id": str(uuid4()),
        "user_id": str(user_id),
        "conversation_id": str(conversation_id),
        "action_type": "calendar.create_event",
        "status": "pending",
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        "parameters": {"summary": "Gym"},
    }
    history = [{
        "role": "assistant",
        "metadata": {"action_approval": proposal},
    }]

    assert ChatService._pending_calendar_create_proposal(
        history=history,
        user_id=user_id,
        conversation_id=conversation_id,
        now=datetime.now(timezone.utc),
    ) == proposal

    proposal["status"] = "approved"
    assert ChatService._pending_calendar_create_proposal(
        history=history,
        user_id=user_id,
        conversation_id=conversation_id,
        now=datetime.now(timezone.utc),
    ) == proposal


def test_pending_calendar_create_draft_restores_and_explicitly_clears():
    draft = {
        "summary": "Planning",
        "event_date": "2026-11-10",
        "start_time": "10:00",
        "time_zone": "Asia/Kolkata",
        "calendar_name": "Work Calendar",
    }
    history = [
        {
            "role": "assistant",
            "metadata": {"calendar_create_draft": draft},
        },
        {"role": "user", "content": "10:30 AM"},
        {"role": "assistant", "metadata": {"provider": ""}},
    ]

    assert ChatService._pending_calendar_create_draft(history) == draft

    history.append({
        "role": "assistant",
        "metadata": {
            "action_approval": {
                "action_type": "calendar.create_event",
                "status": "pending",
            },
        },
    })
    assert ChatService._pending_calendar_create_draft(history) is None


def test_calendar_approval_and_execute_are_separate_api_actions(monkeypatch):
    connection_id = uuid4()
    payload = ApprovalProposalCreate.model_validate({
        "conversation_id": uuid4(),
        "source_message_id": uuid4(),
        "connection_id": connection_id,
        "action_type": "calendar.create_event",
        "parameters": {
            "calendar_id": "primary",
            "summary": "Calendar approval test",
            "start": "2026-11-10T10:00:00+05:30",
            "end": "2026-11-10T10:30:00+05:30",
            "time_zone": "Asia/Kolkata",
            "all_day": False,
        },
    })
    pending = _proposal(USER_ID, payload)
    approved = pending.model_copy(update={"status": "approved"})
    completed = approved.model_copy(update={"status": "completed"})

    class ServiceStub:
        db = object()

        def approve(self, **_kwargs):
            return approved

        def get_proposal(self, **_kwargs):
            return approved

    service = ServiceStub()
    executor = SimpleNamespace(execute=AsyncMock(return_value=completed))
    executor_factory = Mock(return_value=executor)
    executor_factory.SUPPORTED_ACTION_TYPES = {"calendar.create_event"}
    monkeypatch.setattr(
        action_approvals_endpoint,
        "CalendarActionExecutor",
        executor_factory,
    )
    app.dependency_overrides[deps.get_current_user] = lambda: {
        "id": str(USER_ID),
    }
    app.dependency_overrides[get_action_approval_service] = lambda: service

    try:
        client = TestClient(app)
        approval_response = client.post(
            f"/api/v1/action-approvals/{pending.proposal_id}/approve",
        )
        assert approval_response.status_code == 200
        assert approval_response.json()["proposal"]["status"] == "approved"
        executor_factory.assert_not_called()
        executor.execute.assert_not_awaited()

        execute_response = client.post(
            f"/api/v1/action-approvals/{pending.proposal_id}/execute",
        )
        assert execute_response.status_code == 200
        assert execute_response.json()["proposal"]["status"] == "completed"
        executor.execute.assert_awaited_once_with(
            user_id=USER_ID,
            proposal_id=pending.proposal_id,
        )
    finally:
        app.dependency_overrides.clear()


def test_calendar_create_proposal_persists_gym_with_timezone_and_ownership(
    monkeypatch,
):
    for flag in (
        "CALENDAR_CREATE_EVENT_ENABLED",
        "CALENDAR_UPDATE_EVENT_ENABLED",
        "CALENDAR_DELETE_EVENT_ENABLED",
    ):
        monkeypatch.setattr(
            f"app.services.calendar_action_executor.settings.{flag}",
            False,
        )
    user_id = uuid4()
    connection_id = uuid4()
    proposal_id = uuid4()
    payload = ApprovalProposalCreate.model_validate({
        "conversation_id": uuid4(),
        "source_message_id": uuid4(),
        "connection_id": connection_id,
        "action_type": "calendar.create_event",
        "parameters": {
            "calendar_id": "primary",
            "summary": "Gym",
            "start": "2026-11-08T10:00:00+05:30",
            "end": "2026-11-08T11:00:00+05:30",
            "time_zone": "Asia/Kolkata",
            "all_day": False,
        },
    })
    repository_calls = []
    checked_connections = []

    def validate_connection(*, user_id, connection_id, action_type):
        checked_connections.append((user_id, connection_id, action_type))
        return SimpleNamespace(email="owner@example.com")

    class RepositoryStub:
        def create_proposal(self, **kwargs):
            repository_calls.append(kwargs)
            identity = build_calendar_operation_identity(
                proposal_id=kwargs["proposal_id"],
                user_id=kwargs["user_id"],
                connection_id=kwargs["connection_id"],
                action_type=kwargs["action_type"],
                parameters=kwargs["parameters"],
            )
            now = datetime.now(timezone.utc)
            return {
                "proposal_id": kwargs["proposal_id"],
                "message_id": kwargs["proposal_id"],
                "source_message_id": kwargs["source_message_id"],
                "user_id": kwargs["user_id"],
                "conversation_id": kwargs["conversation_id"],
                "action_type": kwargs["action_type"],
                "parameters": kwargs["parameters"],
                "connection_id": kwargs["connection_id"],
                "account_email": kwargs["account_email"],
                "created_at": now,
                "expires_at": now + timedelta(minutes=15),
                "status": "pending",
                "integrity_schema_version": (
                    identity.integrity_schema_version
                ),
                "payload_fingerprint": identity.payload_fingerprint,
                "operation_id": identity.operation_id,
                "calendar_event_id": identity.event_id,
            }

    service = object.__new__(ActionApprovalService)
    service._validate_connection = validate_connection
    service.repository = RepositoryStub()

    proposal = service.create_proposal(
        user_id=user_id,
        payload=payload,
        proposal_id=proposal_id,
    )

    assert checked_connections == [(
        user_id,
        connection_id,
        "calendar.create_event",
    )]
    saved = repository_calls[0]
    assert saved["user_id"] == user_id
    assert saved["conversation_id"] == payload.conversation_id
    assert saved["source_message_id"] == payload.source_message_id
    assert saved["connection_id"] == connection_id
    assert saved["action_type"] == "calendar.create_event"
    assert saved["parameters"]["summary"] == "Gym"
    assert saved["parameters"]["start"] == "2026-11-08T10:00:00+05:30"
    assert saved["parameters"]["end"] == "2026-11-08T11:00:00+05:30"
    assert saved["parameters"]["time_zone"] == "Asia/Kolkata"
    assert saved["calendar_operation"]["user_id"] == str(user_id)
    assert saved["calendar_operation"]["connection_id"] == str(connection_id)
    assert proposal.status == "pending"
    assert proposal.execution_available is False
    assert proposal.parameters["summary"] == "Gym"
    assert proposal.parameters["time_zone"] == "Asia/Kolkata"


@pytest.mark.parametrize(
    ("action_type", "flag"),
    [
        ("calendar.create_event", "CALENDAR_CREATE_EVENT_ENABLED"),
        ("calendar.update_event", "CALENDAR_UPDATE_EVENT_ENABLED"),
        ("calendar.delete_event", "CALENDAR_DELETE_EVENT_ENABLED"),
    ],
)
def test_calendar_proposal_reports_execution_availability_from_flag(
    monkeypatch,
    action_type,
    flag,
):
    now = datetime.now(timezone.utc)
    proposal = ActionApprovalProposal(
        proposal_id=uuid4(),
        message_id=uuid4(),
        source_message_id=uuid4(),
        user_id=uuid4(),
        conversation_id=uuid4(),
        action_type=action_type,
        parameters={},
        connection_id=uuid4(),
        created_at=now,
        expires_at=now + timedelta(minutes=15),
        status="approved",
    )
    monkeypatch.setattr(
        f"app.services.action_approval_service.settings.{flag}",
        False,
    )
    disabled = ActionApprovalService._with_execution_availability(proposal)
    assert disabled.execution_available is False

    monkeypatch.setattr(
        f"app.services.action_approval_service.settings.{flag}",
        True,
    )
    enabled = ActionApprovalService._with_execution_availability(proposal)
    assert enabled.execution_available is True


def test_calendar_create_proposal_rejects_missing_required_fields():
    with pytest.raises(ValidationError):
        ApprovalProposalCreate.model_validate({
            "conversation_id": uuid4(),
            "source_message_id": uuid4(),
            "connection_id": uuid4(),
            "action_type": "calendar.create_event",
            "parameters": {
                "calendar_id": "primary",
                "summary": "Gym",
                "start": "2026-11-08T10:00:00+05:30",
                "time_zone": "Asia/Kolkata",
                "all_day": False,
            },
        })


def test_calendar_create_proposal_surfaces_repository_failure():
    user_id = uuid4()
    connection_id = uuid4()
    payload = ApprovalProposalCreate.model_validate({
        "conversation_id": uuid4(),
        "source_message_id": uuid4(),
        "connection_id": connection_id,
        "action_type": "calendar.create_event",
        "parameters": {
            "calendar_id": "primary",
            "summary": "Gym",
            "start": "2026-11-08T10:00:00+05:30",
            "end": "2026-11-08T11:00:00+05:30",
            "time_zone": "Asia/Kolkata",
            "all_day": False,
        },
    })

    class RepositoryStub:
        def create_proposal(self, **_kwargs):
            raise RuntimeError("database unavailable")

    service = object.__new__(ActionApprovalService)
    service._validate_connection = lambda **_kwargs: SimpleNamespace(
        email="owner@example.com",
    )
    service.repository = RepositoryStub()

    with pytest.raises(RuntimeError, match="database unavailable"):
        service.create_proposal(user_id=user_id, payload=payload)


def test_unowned_connection_is_rejected_before_credentials_are_checked():
    service = object.__new__(ActionApprovalService)
    user_id = uuid4()
    connection_id = uuid4()
    ownership_checks = []

    class ConnectionRepositoryStub:
        def get_active_google_connection(self, *, user_id, connection_id):
            ownership_checks.append((user_id, connection_id))
            return None

    class CredentialRepositoryStub:
        def get_by_connection(self, **_kwargs):
            pytest.fail(
                "Credentials must not be checked for an unowned connection.",
            )

    service.connection_repository = ConnectionRepositoryStub()
    service.credential_repository = CredentialRepositoryStub()

    with pytest.raises(LookupError, match="Connected Google account"):
        service._validate_connection(
            user_id=user_id,
            connection_id=connection_id,
            action_type="gmail.send",
        )

    assert ownership_checks == [(user_id, connection_id)]
