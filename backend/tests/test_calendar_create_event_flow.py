import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from unittest.mock import AsyncMock, Mock

from app.schemas.calendar_approval import CalendarCreateEventParameters
from app.services.calendar_action_executor import CalendarActionExecutor
from app.services.calendar_action_identity import (
    build_calendar_operation_identity,
)
from app.workflows import calendar_agent_node as calendar_node_module


USER_ID = uuid4()
CONNECTION_ID = uuid4()
PROPOSAL_ID = uuid4()
PARAMETERS = {
    "calendar_id": "primary",
    "summary": "Design review",
    "description": None,
    "start": "2026-06-10T10:00:00-07:00",
    "end": "2026-06-10T11:00:00-07:00",
    "time_zone": "America/Los_Angeles",
    "location": None,
    "attendees": [],
    "all_day": False,
}


class SessionContext:
    def __enter__(self):
        return object()

    def __exit__(self, *_args):
        return False


def _calendar_state(write_enabled: bool):
    return {
        "user_message": "Add a design review to my calendar tomorrow.",
        "gmail_plan": {
            "calendar_tool": "create_calendar_event_proposal",
            "calendar_arguments": PARAMETERS,
        },
        "assistant_capabilities": {
            "google_calendar": {
                "write_execution_available": write_enabled,
            },
        },
    }


def test_calendar_create_proposal_is_not_prepared_without_write_capability(
    monkeypatch,
):
    access_check = AsyncMock()
    monkeypatch.setattr(
        calendar_node_module,
        "CalendarService",
        lambda _db: SimpleNamespace(
            validate_create_event_access=access_check,
        ),
    )
    monkeypatch.setattr(
        calendar_node_module,
        "SessionLocal",
        SessionContext,
    )
    context = SimpleNamespace(
        context={
            "user_id": str(USER_ID),
            "calendar_write_connection_id": str(CONNECTION_ID),
            "google_connection_available": True,
        }
    )

    result = calendar_node_module.calendar_agent_node(
        _calendar_state(False),
        context,
    )

    assert "calendar_create_event_proposal" not in result
    access_check.assert_not_awaited()


def test_calendar_create_proposal_preflights_access_and_returns_payload(
    monkeypatch,
):
    access_check = AsyncMock()
    monkeypatch.setattr(
        calendar_node_module,
        "CalendarService",
        lambda _db: SimpleNamespace(
            validate_create_event_access=access_check,
        ),
    )
    monkeypatch.setattr(
        calendar_node_module,
        "SessionLocal",
        SessionContext,
    )
    context = SimpleNamespace(
        context={
            "user_id": str(USER_ID),
            "calendar_write_connection_id": str(CONNECTION_ID),
            "google_connection_available": True,
        }
    )

    result = calendar_node_module.calendar_agent_node(
        _calendar_state(True),
        context,
    )

    assert result["calendar_create_event_proposal"] == PARAMETERS
    access_check.assert_awaited_once_with(
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        calendar_id="primary",
    )


def test_revised_calendar_proposal_carries_superseded_proposal_id(
    monkeypatch,
):
    access_check = AsyncMock()
    monkeypatch.setattr(
        calendar_node_module,
        "CalendarService",
        lambda _db: SimpleNamespace(
            validate_create_event_access=access_check,
        ),
    )
    monkeypatch.setattr(
        calendar_node_module,
        "SessionLocal",
        SessionContext,
    )
    state = _calendar_state(True)
    state["gmail_plan"]["supersedes_proposal_id"] = str(PROPOSAL_ID)
    context = SimpleNamespace(
        context={
            "user_id": str(USER_ID),
            "calendar_write_connection_id": str(CONNECTION_ID),
            "google_connection_available": True,
        }
    )

    result = calendar_node_module.calendar_agent_node(state, context)

    assert result["calendar_proposal_supersedes_id"] == str(PROPOSAL_ID)
    assert result["calendar_create_event_proposal"] == PARAMETERS


def test_calendar_create_proposal_is_prepared_while_execution_flag_is_disabled(
    monkeypatch,
):
    access_check = AsyncMock()
    monkeypatch.setattr(
        calendar_node_module,
        "CalendarService",
        lambda _db: SimpleNamespace(
            validate_create_event_access=access_check,
        ),
    )
    monkeypatch.setattr(
        calendar_node_module,
        "SessionLocal",
        SessionContext,
    )
    state = _calendar_state(False)
    state["assistant_capabilities"]["google_calendar"] = {
        "write_permission_granted": True,
        "create_execution_available": False,
        "create_feature_enabled": False,
    }
    context = SimpleNamespace(
        context={
            "user_id": str(USER_ID),
            "calendar_write_connection_id": str(CONNECTION_ID),
            "google_connection_available": True,
        }
    )

    result = calendar_node_module.calendar_agent_node(state, context)

    assert result["calendar_create_event_proposal"] == PARAMETERS
    assert "approval request" in result["assistant_response"]
    assert "unless you approve and separately execute" in (
        result["assistant_response"]
    )
    access_check.assert_awaited_once_with(
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        calendar_id="primary",
    )


def test_calendar_create_without_end_time_asks_for_end_time():
    state = _calendar_state(True)
    state["user_message"] = (
        "Add gym in my calendar tomorrow at 10 AM."
    )
    state["gmail_plan"]["calendar_arguments"] = None
    context = SimpleNamespace(
        context={
            "user_id": str(USER_ID),
            "calendar_write_connection_id": str(CONNECTION_ID),
            "google_connection_available": True,
        }
    )

    result = calendar_node_module.calendar_agent_node(state, context)

    assert "What end time should I use" in result["assistant_response"]
    assert "calendar_create_event_proposal" not in result


def test_calendar_executor_rechecks_write_access_before_claim(
    monkeypatch,
):
    monkeypatch.setattr(
        "app.services.calendar_action_executor.settings.CALENDAR_CREATE_EVENT_ENABLED",
        True,
    )
    parameters = CalendarCreateEventParameters.model_validate(PARAMETERS)
    identity = build_calendar_operation_identity(
        proposal_id=PROPOSAL_ID,
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        action_type="calendar.create_event",
        parameters=parameters.model_dump(mode="json"),
    )
    proposal = SimpleNamespace(
        proposal_id=PROPOSAL_ID,
        action_type="calendar.create_event",
        status="approved",
        parameters=parameters.model_dump(mode="json"),
        connection_id=CONNECTION_ID,
        operation_id=identity.operation_id,
        payload_fingerprint=identity.payload_fingerprint,
        calendar_event_id=identity.event_id,
    )
    approval_service = SimpleNamespace(
        get_proposal=Mock(return_value=proposal),
        claim_calendar_execution=Mock(),
        calendar_operation_repository=Mock(),
    )
    calendar_service = SimpleNamespace(
        validate_create_event_access=AsyncMock(
            side_effect=PermissionError("write access revoked"),
        )
    )
    api_client = SimpleNamespace(request_mutation=AsyncMock())
    executor = CalendarActionExecutor(
        db=None,
        approval_service=approval_service,
        calendar_service=calendar_service,
        api_client=api_client,
    )

    with pytest.raises(PermissionError, match="write access revoked"):
        asyncio.run(
            executor.execute(
                user_id=USER_ID,
                proposal_id=PROPOSAL_ID,
            )
        )

    approval_service.claim_calendar_execution.assert_not_called()
    api_client.request_mutation.assert_not_awaited()
