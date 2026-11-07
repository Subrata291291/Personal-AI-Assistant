import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.calendar_approval import (
    CalendarCreateEventParameters,
    CalendarDeleteEventParameters,
    CalendarUpdateEventParameters,
)
from app.integrations.google.exceptions import GoogleAPIError
from app.integrations.connectors.permissions.approval_policy import (
    ApprovalPolicy,
)
from app.services.calendar_action_executor import CalendarActionExecutor
from app.services.calendar_action_identity import (
    build_calendar_operation_identity,
)


USER_ID = uuid4()
CONNECTION_ID = uuid4()
PROPOSAL_ID = uuid4()
START = datetime(2026, 6, 10, 10, tzinfo=timezone.utc)
END = datetime(2026, 6, 10, 11, tzinfo=timezone.utc)
CURRENT_EVENT = {
    "summary": "Design review",
    "description": None,
    "location": None,
    "start": {
        "dateTime": START.isoformat(),
        "timeZone": "UTC",
    },
    "end": {
        "dateTime": END.isoformat(),
        "timeZone": "UTC",
    },
    "attendees": [],
    "etag": '"etag-1"',
}


def _normalized_event(summary="Design review", etag='"etag-1"'):
    return {
        "id": "event-123",
        "summary": summary,
        "description": None,
        "location": None,
        "etag": etag,
        "start": {
            "date": None,
            "date_time": START,
            "time_zone": "UTC",
        },
        "end": {
            "date": None,
            "date_time": END,
            "time_zone": "UTC",
        },
        "attendees": [],
        "html_link": None,
    }


def _make_proposal(action_type, parameters):
    identity = build_calendar_operation_identity(
        proposal_id=PROPOSAL_ID,
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        action_type=action_type,
        parameters=parameters,
    )
    return SimpleNamespace(
        proposal_id=PROPOSAL_ID,
        action_type=action_type,
        status="approved",
        parameters=parameters,
        connection_id=CONNECTION_ID,
        operation_id=identity.operation_id,
        payload_fingerprint=identity.payload_fingerprint,
        calendar_event_id=identity.event_id,
        execution_started_at=None,
    )


def _executor(monkeypatch, action_type, parameters, readback):
    flag = {
        "calendar.create_event": "CALENDAR_CREATE_EVENT_ENABLED",
        "calendar.update_event": "CALENDAR_UPDATE_EVENT_ENABLED",
        "calendar.delete_event": "CALENDAR_DELETE_EVENT_ENABLED",
    }[action_type]
    monkeypatch.setattr(
        f"app.services.calendar_action_executor.settings.{flag}",
        True,
    )
    proposal = _make_proposal(action_type, parameters)
    completed = SimpleNamespace(status="completed")
    approval = SimpleNamespace(
        get_proposal=Mock(return_value=proposal),
        claim_calendar_execution=Mock(return_value=proposal),
        finish_calendar_execution=Mock(return_value=completed),
        calendar_operation_repository=Mock(),
    )
    calendar = SimpleNamespace(
        validate_create_event_access=AsyncMock(),
        validate_event_write_access=AsyncMock(
            return_value=_normalized_event(),
        ),
        _google_get=AsyncMock(),
        _normalize_event=Mock(),
    )
    api = SimpleNamespace(request_mutation=AsyncMock(return_value={}))
    monkeypatch.setattr(
        "app.services.calendar_action_executor.SessionLocal",
        lambda: SimpleNamespace(close=Mock()),
    )
    executor = CalendarActionExecutor(
        db=None,
        approval_service=approval,
        calendar_service=calendar,
        api_client=api,
    )
    executor._read_event_for_reconciliation = AsyncMock(
        return_value=readback,
    )
    return executor, approval, api, proposal, completed


def test_update_proposal_binds_snapshot_and_expected_etag():
    parameters = CalendarUpdateEventParameters.model_validate({
        "calendar_id": "primary",
        "event_id": "event-123",
        "expected_etag": '"etag-1"',
        "current_event": CURRENT_EVENT,
        "changes": {"summary": "Updated design review"},
    })

    assert parameters.changes == {"summary": "Updated design review"}

    with pytest.raises(ValidationError, match="snapshot is invalid"):
        CalendarUpdateEventParameters.model_validate({
            "calendar_id": "primary",
            "event_id": "event-123",
            "expected_etag": '"etag-2"',
            "current_event": CURRENT_EVENT,
            "changes": {"summary": "Updated design review"},
        })


def test_update_requires_start_and_end_together():
    with pytest.raises(ValidationError, match="updated together"):
        CalendarUpdateEventParameters.model_validate({
            "calendar_id": "primary",
            "event_id": "event-123",
            "expected_etag": '"etag-1"',
            "current_event": CURRENT_EVENT,
            "changes": {
                "start": {
                    "dateTime": "2026-06-10T12:00:00+00:00",
                    "timeZone": "UTC",
                },
            },
        })


def test_timed_update_requires_the_users_iana_timezone_and_matching_offset():
    parameters = {
        "calendar_id": "primary",
        "event_id": "event-123",
        "expected_etag": '"etag-1"',
        "current_event": CURRENT_EVENT,
        "changes": {
            "start": {
                "dateTime": "2026-06-10T10:00:00+05:30",
                "timeZone": "Asia/Kolkata",
            },
            "end": {
                "dateTime": "2026-06-10T11:00:00+05:30",
                "timeZone": "Asia/Kolkata",
            },
        },
        "time_zone": "Asia/Kolkata",
    }

    validated = CalendarUpdateEventParameters.model_validate(parameters)
    assert validated.time_zone == "Asia/Kolkata"

    parameters["time_zone"] = "UTC"
    with pytest.raises(ValidationError, match="match the user's timezone"):
        CalendarUpdateEventParameters.model_validate(parameters)


def test_delete_proposal_requires_exact_etag_snapshot():
    parameters = CalendarDeleteEventParameters.model_validate({
        "calendar_id": "primary",
        "event_id": "event-123",
        "expected_etag": '"etag-1"',
        "current_event": CURRENT_EVENT,
    })
    assert parameters.event_id == "event-123"


def test_approved_update_uses_conditional_patch_and_confirms_readback(
    monkeypatch,
):
    parameters = CalendarUpdateEventParameters.model_validate({
        "calendar_id": "primary",
        "event_id": "event-123",
        "expected_etag": '"etag-1"',
        "current_event": CURRENT_EVENT,
        "changes": {"summary": "Updated design review"},
    }).model_dump(mode="json")
    updated = _normalized_event("Updated design review", '"etag-2"')
    executor, approval, api, _proposal, completed = _executor(
        monkeypatch,
        "calendar.update_event",
        parameters,
        updated,
    )

    result = asyncio.run(
        executor.execute(user_id=USER_ID, proposal_id=PROPOSAL_ID)
    )

    assert result is completed
    approval.claim_calendar_execution.assert_called_once_with(
        user_id=USER_ID,
        proposal_id=PROPOSAL_ID,
    )
    api.request_mutation.assert_awaited_once()
    request = api.request_mutation.call_args.kwargs
    assert request["method"] == "PATCH"
    assert request["headers"] == {"If-Match": '"etag-1"'}
    assert request["json_data"] == {"summary": "Updated design review"}


def test_approved_delete_uses_conditional_delete_and_confirms_absence(
    monkeypatch,
):
    parameters = CalendarDeleteEventParameters.model_validate({
        "calendar_id": "primary",
        "event_id": "event-123",
        "expected_etag": '"etag-1"',
        "current_event": CURRENT_EVENT,
    }).model_dump(mode="json")
    executor, _approval, api, _proposal, completed = _executor(
        monkeypatch,
        "calendar.delete_event",
        parameters,
        None,
    )

    result = asyncio.run(
        executor.execute(user_id=USER_ID, proposal_id=PROPOSAL_ID)
    )

    assert result is completed
    request = api.request_mutation.call_args.kwargs
    assert request["method"] == "DELETE"
    assert request["headers"] == {"If-Match": '"etag-1"'}
    assert request["json_data"] is None


def test_approved_calendar_create_confirms_google_readback_for_saved_payload(
    monkeypatch,
):
    parameters = CalendarCreateEventParameters.model_validate({
        "calendar_id": "primary",
        "summary": "A",
        "description": None,
        "start": "2026-11-08T14:00:00+05:30",
        "end": "2026-11-08T15:00:00+05:30",
        "time_zone": "Asia/Kolkata",
        "location": None,
        "attendees": [],
        "all_day": False,
    }).model_dump(mode="json")
    event = {
        "id": "event-123",
        "summary": "A",
        "description": None,
        "location": None,
        "start": {
            "dateTime": "2026-11-08T14:00:00+05:30",
            "timeZone": "Asia/Kolkata",
        },
        "end": {
            "dateTime": "2026-11-08T15:00:00+05:30",
            "timeZone": "Asia/Kolkata",
        },
        "attendees": [],
    }
    executor, approval, api, proposal, completed = _executor(
        monkeypatch,
        "calendar.create_event",
        parameters,
        event,
    )
    identity = build_calendar_operation_identity(
        proposal_id=PROPOSAL_ID,
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        action_type="calendar.create_event",
        parameters=parameters,
    )
    event["id"] = identity.event_id
    event["extendedProperties"] = {
        "private": {
            "personal_ai_operation_id": str(identity.operation_id),
            "personal_ai_payload_fingerprint": identity.payload_fingerprint,
        },
    }
    proposal.action_type = "calendar.create_event"
    proposal.parameters = parameters
    proposal.operation_id = identity.operation_id
    proposal.payload_fingerprint = identity.payload_fingerprint
    proposal.calendar_event_id = identity.event_id
    executor._read_back = AsyncMock(return_value=event)

    result = asyncio.run(
        executor.execute(user_id=USER_ID, proposal_id=PROPOSAL_ID)
    )

    assert result is completed
    request = api.request_mutation.call_args.kwargs
    assert request["method"] == "POST"
    assert "/calendars/primary/events" in request["url"]
    assert request["json_data"]["summary"] == "A"
    assert request["json_data"]["start"] == {
        "dateTime": "2026-11-08T14:00:00+05:30",
        "timeZone": "Asia/Kolkata",
    }
    assert request["json_data"]["end"] == {
        "dateTime": "2026-11-08T15:00:00+05:30",
        "timeZone": "Asia/Kolkata",
    }
    assert approval.finish_calendar_execution.call_args.kwargs[
        "state"
    ] == "completed"
    assert approval.finish_calendar_execution.call_args.kwargs[
        "safe_result"
    ]["outcome"] == "created"


def test_google_calendar_create_failure_is_persisted_as_failed(
    monkeypatch,
):
    parameters = CalendarCreateEventParameters.model_validate({
        "calendar_id": "primary",
        "summary": "A",
        "start": "2026-11-08T14:00:00+05:30",
        "end": "2026-11-08T15:00:00+05:30",
        "time_zone": "Asia/Kolkata",
        "all_day": False,
    }).model_dump(mode="json")
    executor, approval, api, proposal, _completed = _executor(
        monkeypatch,
        "calendar.create_event",
        parameters,
        None,
    )
    identity = build_calendar_operation_identity(
        proposal_id=PROPOSAL_ID,
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        action_type="calendar.create_event",
        parameters=parameters,
    )
    proposal.action_type = "calendar.create_event"
    proposal.parameters = parameters
    proposal.operation_id = identity.operation_id
    proposal.payload_fingerprint = identity.payload_fingerprint
    proposal.calendar_event_id = identity.event_id
    api.request_mutation.side_effect = GoogleAPIError(
        status_code=400,
        message="invalid event",
    )

    asyncio.run(
        executor.execute(user_id=USER_ID, proposal_id=PROPOSAL_ID)
    )

    finish_arguments = approval.finish_calendar_execution.call_args.kwargs
    assert finish_arguments["state"] == "failed"
    assert finish_arguments["safe_result"] == {
        "outcome": "rejected",
        "provider_status_code": 400,
    }


@pytest.mark.parametrize(
    ("action_type", "parameters", "flag"),
    [
        (
            "calendar.create_event",
            {},
            "CALENDAR_CREATE_EVENT_ENABLED",
        ),
        (
            "calendar.update_event",
            {},
            "CALENDAR_UPDATE_EVENT_ENABLED",
        ),
        (
            "calendar.delete_event",
            {},
            "CALENDAR_DELETE_EVENT_ENABLED",
        ),
    ],
)
def test_calendar_mutations_are_blocked_when_feature_flag_is_disabled(
    monkeypatch,
    action_type,
    parameters,
    flag,
):
    monkeypatch.setattr(
        f"app.services.calendar_action_executor.settings.{flag}",
        False,
    )
    approval = SimpleNamespace(
        get_proposal=Mock(return_value=SimpleNamespace(
            action_type=action_type,
        )),
        calendar_operation_repository=Mock(),
    )
    api = SimpleNamespace(request_mutation=AsyncMock())
    executor = CalendarActionExecutor(
        db=None,
        approval_service=approval,
        calendar_service=SimpleNamespace(),
        api_client=api,
    )

    with pytest.raises(PermissionError, match="execution is currently disabled"):
        asyncio.run(
            executor.execute(user_id=USER_ID, proposal_id=PROPOSAL_ID)
        )

    api.request_mutation.assert_not_awaited()


@pytest.mark.parametrize(
    "capability",
    (
        "create_calendar_event",
        "update_calendar_event",
        "delete_calendar_event",
    ),
)
def test_every_calendar_write_requires_explicit_approval(capability):
    decision = ApprovalPolicy().check_approval(capability)

    assert decision.required is True
    assert "explicit user approval" in decision.reason
