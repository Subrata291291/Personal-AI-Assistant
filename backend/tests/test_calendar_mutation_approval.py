import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.schemas.action_approval import ApprovalProposalCreate
from app.schemas.calendar_approval import (
    CalendarCreateEventParameters,
    CalendarDeleteEventParameters,
    CalendarUpdateEventParameters,
)
from app.integrations.google.exceptions import (
    GoogleAPIError,
    GoogleAPIMutationError,
)
from app.integrations.connectors.permissions.approval_policy import (
    ApprovalPolicy,
)
from app.services.action_approval_service import (
    ActionApprovalService,
    ApprovalConflictError,
)
from app.services.calendar_action_executor import (
    CalendarActionExecutor,
    _calendar_identity_parameters,
)
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
        "current_event": {
            **CURRENT_EVENT,
            "recurrence": ["RRULE:FREQ=DAILY"],
        },
        "changes": {"summary": "Updated design review"},
    })

    assert parameters.changes == {"summary": "Updated design review"}

    with pytest.raises(ValidationError, match="snapshot is invalid"):
        CalendarUpdateEventParameters.model_validate({
            "calendar_id": "primary",
            "event_id": "event-123",
            "expected_etag": '"etag-2"',
            "current_event": {
                **CURRENT_EVENT,
                "recurrence": ["RRULE:FREQ=DAILY"],
            },
            "changes": {"summary": "Updated design review"},
        })


def test_update_requires_start_and_end_together():
    with pytest.raises(ValidationError, match="updated together"):
        CalendarUpdateEventParameters.model_validate({
            "calendar_id": "primary",
            "event_id": "event-123",
            "expected_etag": '"etag-1"',
            "current_event": {
                **CURRENT_EVENT,
                "recurrence": ["RRULE:FREQ=DAILY"],
            },
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


def test_series_delete_proposal_requires_and_persists_master_recurrence():
    with pytest.raises(ValidationError, match="entire series"):
        CalendarDeleteEventParameters.model_validate({
            "calendar_id": "primary",
            "event_id": "event-123",
            "expected_etag": '"etag-1"',
            "current_event": CURRENT_EVENT,
            "delete_entire_series": True,
        })

    snapshot = {
        **CURRENT_EVENT,
        "recurrence": ["RRULE:FREQ=DAILY"],
    }
    parameters = CalendarDeleteEventParameters.model_validate({
        "calendar_id": "primary",
        "event_id": "series-master",
        "expected_etag": '"etag-1"',
        "current_event": snapshot,
        "delete_entire_series": True,
    })

    assert parameters.delete_entire_series is True
    assert parameters.current_event["recurrence"] == ["RRULE:FREQ=DAILY"]

    with pytest.raises(ValidationError, match="explicit series scope"):
        CalendarDeleteEventParameters.model_validate({
            "calendar_id": "primary",
            "event_id": "series-master",
            "expected_etag": '"etag-1"',
            "current_event": snapshot,
        })


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
    assert request["url"].endswith(
        "/calendars/primary/events/event-123"
    )
    assert request["headers"] == {"If-Match": '"etag-1"'}
    assert request["json_data"] == {"summary": "Updated design review"}
    assert "recurrence" not in request["json_data"]


def test_title_only_update_preserves_recurrence_for_legacy_saved_snapshot(
    monkeypatch,
):
    parameters = CalendarUpdateEventParameters.model_validate({
        "calendar_id": "primary",
        "event_id": "event-123",
        "expected_etag": '"etag-1"',
        "current_event": CURRENT_EVENT,
        "changes": {"summary": "Updated design review"},
    }).model_dump(mode="json")
    readback = _normalized_event("Updated design review", '"etag-2"')
    readback["recurrence"] = ["RRULE:FREQ=WEEKLY"]
    executor, _approval, api, _proposal, completed = _executor(
        monkeypatch,
        "calendar.update_event",
        parameters,
        readback,
    )
    executor.calendar_service.validate_event_write_access.return_value[
        "recurrence"
    ] = ["RRULE:FREQ=WEEKLY"]

    result = asyncio.run(
        executor.execute(user_id=USER_ID, proposal_id=PROPOSAL_ID)
    )

    assert result is completed
    request = api.request_mutation.call_args.kwargs
    assert request["method"] == "PATCH"
    assert request["headers"] == {"If-Match": '"etag-1"'}
    assert request["json_data"] == {"summary": "Updated design review"}


def test_update_readback_verifies_unchanged_snapshot_fields():
    updated = _normalized_event("Updated design review")
    changes = {"summary": "Updated design review"}

    assert CalendarActionExecutor._event_matches_update(
        updated,
        changes,
        CURRENT_EVENT,
    )

    updated["location"] = "Unexpected change"
    assert not CalendarActionExecutor._event_matches_update(
        updated,
        changes,
        CURRENT_EVENT,
    )


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


def test_approved_series_delete_uses_master_id_and_conditional_etag(
    monkeypatch,
):
    snapshot = {
        **CURRENT_EVENT,
        "recurrence": ["RRULE:FREQ=DAILY"],
    }
    parameters = CalendarDeleteEventParameters.model_validate({
        "calendar_id": "primary",
        "event_id": "series-master",
        "expected_etag": '"etag-1"',
        "current_event": snapshot,
        "delete_entire_series": True,
    }).model_dump(mode="json")
    executor, _approval, api, _proposal, completed = _executor(
        monkeypatch,
        "calendar.delete_event",
        parameters,
        None,
    )
    executor.calendar_service.validate_event_write_access.return_value[
        "recurrence"
    ] = ["RRULE:FREQ=DAILY"]

    result = asyncio.run(
        executor.execute(user_id=USER_ID, proposal_id=PROPOSAL_ID)
    )

    assert result is completed
    request = api.request_mutation.call_args.kwargs
    assert request["method"] == "DELETE"
    assert request["url"].endswith(
        "/calendars/primary/events/series-master"
    )
    assert request["headers"] == {"If-Match": '"etag-1"'}


def test_uncertain_delete_reconciles_verified_absence_as_completed(
    monkeypatch,
):
    parameters = CalendarDeleteEventParameters.model_validate({
        "calendar_id": "primary",
        "event_id": "event-123",
        "expected_etag": '"etag-1"',
        "current_event": CURRENT_EVENT,
    }).model_dump(mode="json")
    executor, approval, _api, proposal, completed = _executor(
        monkeypatch,
        "calendar.delete_event",
        parameters,
        None,
    )
    proposal.status = "execution_uncertain"
    executor._finish = Mock(return_value=completed)

    result = asyncio.run(
        executor._reconcile_update_or_delete(
            user_id=USER_ID,
            proposal=proposal,
        )
    )

    assert result is completed
    executor._finish.assert_called_once_with(
        user_id=USER_ID,
        proposal_id=PROPOSAL_ID,
        expected_state="execution_uncertain",
        state="completed",
        classification="confirmed",
        reconciliation="confirmed",
        result={
            "outcome": "deleted",
            "google_event_id": "event-123",
        },
    )
    update_reconciliation = (
        approval.calendar_operation_repository.update_uncertain_reconciliation
    )
    update_reconciliation.assert_not_called()


def test_uncertain_recurring_master_delete_confirms_cancelled_master(
    monkeypatch,
):
    parameters = CalendarDeleteEventParameters.model_validate({
        "calendar_id": "primary",
        "event_id": "series-master",
        "expected_etag": '"etag-1"',
        "current_event": {
            **CURRENT_EVENT,
            "recurrence": ["RRULE:FREQ=DAILY"],
        },
        "delete_entire_series": True,
    }).model_dump(mode="json")
    cancelled_master = {
        **_normalized_event(),
        "id": "series-master",
        "status": "cancelled",
    }
    executor, approval, api, proposal, completed = _executor(
        monkeypatch,
        "calendar.delete_event",
        parameters,
        cancelled_master,
    )
    proposal.status = "execution_uncertain"
    executor._finish = Mock(return_value=completed)

    result = asyncio.run(
        executor._reconcile_update_or_delete(
            user_id=USER_ID,
            proposal=proposal,
        )
    )

    assert result is completed
    executor._read_event_for_reconciliation.assert_awaited_once_with(
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        calendar_id="primary",
        event_id="series-master",
    )
    executor._finish.assert_called_once_with(
        user_id=USER_ID,
        proposal_id=PROPOSAL_ID,
        expected_state="execution_uncertain",
        state="completed",
        classification="confirmed",
        reconciliation="confirmed",
        result={
            "outcome": "deleted",
            "google_event_id": "series-master",
        },
    )
    api.request_mutation.assert_not_awaited()
    update_reconciliation = (
        approval.calendar_operation_repository.update_uncertain_reconciliation
    )
    update_reconciliation.assert_not_called()


def test_execution_reconciliation_confirms_cancelled_recurring_master(
    monkeypatch,
):
    parameters = CalendarDeleteEventParameters.model_validate({
        "calendar_id": "primary",
        "event_id": "series-master",
        "expected_etag": '"etag-1"',
        "current_event": {
            **CURRENT_EVENT,
            "recurrence": ["RRULE:FREQ=DAILY"],
        },
        "delete_entire_series": True,
    })
    cancelled_master = {
        **_normalized_event(),
        "id": "series-master",
        "status": "cancelled",
    }
    executor, _approval, api, proposal, completed = _executor(
        monkeypatch,
        "calendar.delete_event",
        parameters.model_dump(mode="json"),
        cancelled_master,
    )
    executor._finish = Mock(return_value=completed)

    result = asyncio.run(
        executor._reconcile_mutation(
            user_id=USER_ID,
            proposal=proposal,
            parameters=parameters,
        )
    )

    assert result is completed
    executor._finish.assert_called_once_with(
        user_id=USER_ID,
        proposal_id=PROPOSAL_ID,
        expected_state="executing",
        state="completed",
        classification="confirmed",
        reconciliation="confirmed",
        result={
            "outcome": "deleted",
            "google_event_id": "series-master",
        },
    )
    api.request_mutation.assert_not_awaited()


@pytest.mark.parametrize("status_code", [404, 410])
def test_event_reconciliation_treats_provider_gone_as_verified_absence(
    monkeypatch,
    status_code,
):
    parameters = CalendarDeleteEventParameters.model_validate({
        "calendar_id": "primary",
        "event_id": "series-master",
        "expected_etag": '"etag-1"',
        "current_event": {
            **CURRENT_EVENT,
            "recurrence": ["RRULE:FREQ=DAILY"],
        },
        "delete_entire_series": True,
    }).model_dump(mode="json")
    executor, _approval, _api, _proposal, _completed = _executor(
        monkeypatch,
        "calendar.delete_event",
        parameters,
        None,
    )
    executor._read_event_for_reconciliation = (
        CalendarActionExecutor._read_event_for_reconciliation.__get__(
            executor,
        )
    )
    executor.calendar_service._google_get.side_effect = GoogleAPIError(
        status_code=status_code,
        message="The event is no longer available.",
    )

    result = asyncio.run(
        executor._read_event_for_reconciliation(
            user_id=USER_ID,
            connection_id=CONNECTION_ID,
            calendar_id="primary",
            event_id="series-master",
        )
    )

    assert result is None
    executor.calendar_service._google_get.assert_awaited_once_with(
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        endpoint="calendars/primary/events/series-master",
        params={},
    )


def test_uncertain_delete_keeps_active_master_pending(monkeypatch):
    parameters = CalendarDeleteEventParameters.model_validate({
        "calendar_id": "primary",
        "event_id": "series-master",
        "expected_etag": '"etag-1"',
        "current_event": {
            **CURRENT_EVENT,
            "recurrence": ["RRULE:FREQ=DAILY"],
        },
        "delete_entire_series": True,
    }).model_dump(mode="json")
    active_master = {
        **_normalized_event(),
        "id": "series-master",
        "status": "confirmed",
        "recurrence": ["RRULE:FREQ=DAILY"],
    }
    executor, approval, api, proposal, _completed = _executor(
        monkeypatch,
        "calendar.delete_event",
        parameters,
        active_master,
    )
    proposal.status = "execution_uncertain"
    proposal.model_copy = lambda *, update: SimpleNamespace(
        **{**vars(proposal), **update},
    )

    result = asyncio.run(
        executor._reconcile_update_or_delete(
            user_id=USER_ID,
            proposal=proposal,
        )
    )

    assert result.status == "execution_uncertain"
    assert result.result == {
        "outcome": "unconfirmed",
        "reason": "event_still_exists",
        "checked_now": True,
    }
    update_reconciliation = (
        approval.calendar_operation_repository.update_uncertain_reconciliation
    )
    update_reconciliation.assert_called_once_with(
        user_id=USER_ID,
        proposal_id=PROPOSAL_ID,
        reconciliation_status="pending",
    )
    api.request_mutation.assert_not_awaited()


def test_uncertain_delete_preserves_uncertainty_when_provider_read_fails(
    monkeypatch,
):
    parameters = CalendarDeleteEventParameters.model_validate({
        "calendar_id": "primary",
        "event_id": "series-master",
        "expected_etag": '"etag-1"',
        "current_event": {
            **CURRENT_EVENT,
            "recurrence": ["RRULE:FREQ=DAILY"],
        },
        "delete_entire_series": True,
    }).model_dump(mode="json")
    executor, approval, api, proposal, _completed = _executor(
        monkeypatch,
        "calendar.delete_event",
        parameters,
        None,
    )
    proposal.status = "execution_uncertain"
    proposal.model_copy = lambda *, update: SimpleNamespace(
        **{**vars(proposal), **update},
    )
    executor._read_event_for_reconciliation.side_effect = GoogleAPIError(
        status_code=503,
        message="Calendar read failed.",
    )

    result = asyncio.run(
        executor._reconcile_update_or_delete(
            user_id=USER_ID,
            proposal=proposal,
        )
    )

    assert result.status == "execution_uncertain"
    assert result.result == {
        "outcome": "unconfirmed",
        "reason": "read_failed",
        "checked_now": True,
    }
    update_reconciliation = (
        approval.calendar_operation_repository.update_uncertain_reconciliation
    )
    update_reconciliation.assert_called_once_with(
        user_id=USER_ID,
        proposal_id=PROPOSAL_ID,
        reconciliation_status="pending",
    )
    api.request_mutation.assert_not_awaited()


def test_delete_404_is_idempotently_confirmed_after_absence_check(monkeypatch):
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
    api.request_mutation.side_effect = GoogleAPIMutationError(
        status_code=404,
        message="The event is already absent.",
        outcome_unknown=False,
        reason="provider_rejected",
    )
    executor._finish = Mock(return_value=completed)

    result = asyncio.run(
        executor.execute(user_id=USER_ID, proposal_id=PROPOSAL_ID)
    )

    assert result is completed
    executor._read_event_for_reconciliation.assert_awaited_once()
    executor._finish.assert_called_once_with(
        user_id=USER_ID,
        proposal_id=PROPOSAL_ID,
        expected_state="executing",
        state="completed",
        classification="confirmed",
        reconciliation="confirmed",
        result={
            "outcome": "deleted",
            "google_event_id": "event-123",
        },
    )


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


def test_recurring_create_sends_and_confirms_daily_recurrence(monkeypatch):
    parameters = CalendarCreateEventParameters.model_validate({
        "calendar_id": "primary",
        "summary": "Gym",
        "description": None,
        "start": "2026-11-10T10:00:00+05:30",
        "end": "2026-11-10T11:00:00+05:30",
        "time_zone": "Asia/Kolkata",
        "location": None,
        "attendees": [],
        "all_day": False,
        "recurrence": ["RRULE:FREQ=DAILY"],
    }).model_dump(mode="json")
    event = {
        "id": "event-123",
        "summary": "Gym",
        "description": None,
        "location": None,
        "start": {
            "dateTime": "2026-11-10T10:00:00+05:30",
            "timeZone": "Asia/Kolkata",
        },
        "end": {
            "dateTime": "2026-11-10T11:00:00+05:30",
            "timeZone": "Asia/Kolkata",
        },
        "attendees": [],
        "recurrence": ["RRULE:FREQ=DAILY"],
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
    assert api.request_mutation.call_args.kwargs["json_data"]["recurrence"] == [
        "RRULE:FREQ=DAILY"
    ]
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


_CREATE_EVENT_PAYLOAD = {
    "calendar_id": "primary",
    "summary": "Design review",
    "description": None,
    "start": "2026-06-10T10:00:00+00:00",
    "end": "2026-06-10T11:00:00+00:00",
    "time_zone": "UTC",
    "location": None,
    "attendees": [],
    "all_day": False,
}


def _create_event_payload(**overrides):
    return {**_CREATE_EVENT_PAYLOAD, **overrides}


class _InMemoryApprovalRepository:
    """Row shapes mirror ActionApprovalRepository for service-level tests."""

    engine = None

    def __init__(self):
        self.row = None
        self.operation = None

    def create_proposal(self, **kwargs):
        operation = kwargs["calendar_operation"]
        now = datetime.now(timezone.utc)
        self.operation = {
            "proposal_id": str(kwargs["proposal_id"]),
            "operation_id": operation["operation_id"],
            "user_id": str(kwargs["user_id"]),
            "connection_id": str(kwargs["connection_id"]),
            "action_type": operation["action_type"],
            "calendar_id": operation["calendar_id"],
            "event_id": operation["event_id"],
            "payload_fingerprint": operation["payload_fingerprint"],
            "execution_state": "pending",
        }
        self.row = {
            "proposal_id": kwargs["proposal_id"],
            "message_id": uuid4(),
            "source_message_id": kwargs["source_message_id"],
            "user_id": kwargs["user_id"],
            "conversation_id": kwargs["conversation_id"],
            "action_type": kwargs["action_type"],
            "parameters": kwargs["parameters"],
            "connection_id": kwargs["connection_id"],
            "account_email": kwargs["account_email"],
            "created_at": now,
            "expires_at": now + timedelta(
                minutes=kwargs["expiration_minutes"]
            ),
            "status": "pending",
            "approved_at": None,
            "execution_started_at": None,
            "completed_at": None,
            "result": None,
            "integrity_schema_version": operation["integrity_schema_version"],
            "payload_fingerprint": operation["payload_fingerprint"],
            "operation_id": UUID(operation["operation_id"]),
            "calendar_event_id": operation["event_id"],
        }
        return self.row

    def recover_stale_gmail_send(self, **kwargs):
        return None

    def get_proposal(self, user_id, proposal_id):
        row = self.row
        if row is None:
            return None
        if row["user_id"] != user_id or row["proposal_id"] != proposal_id:
            return None
        return row

    def transition(
        self,
        *,
        user_id,
        proposal_id,
        expected_status,
        new_status,
        expiry_mode,
        timestamp_field,
    ):
        row = self.row
        if (
            row is None
            or row["user_id"] != user_id
            or row["proposal_id"] != proposal_id
        ):
            return None
        if row["status"] != expected_status:
            return None
        row["status"] = new_status
        row[timestamp_field] = datetime.now(timezone.utc)
        self.operation["execution_state"] = new_status
        return row


class _InMemoryCalendarOperationRepository:
    """Persists the operation state transitions the executor relies on."""

    def __init__(self, repository):
        self.repository = repository

    def get_for_user(self, user_id, proposal_id):
        operation = self.repository.operation
        if operation is None:
            return None
        if (
            operation["user_id"] != str(user_id)
            or operation["proposal_id"] != str(proposal_id)
        ):
            return None
        return dict(operation)

    def claim_execution(self, *, user_id, proposal_id):
        row = self.repository.row
        if row is None or row["status"] != "approved":
            return None
        row["status"] = "executing"
        row["execution_started_at"] = datetime.now(timezone.utc)
        self.repository.operation["execution_state"] = "executing"
        return row

    def finish_execution(
        self,
        *,
        user_id,
        proposal_id,
        expected_state,
        new_state,
        provider_result_classification,
        reconciliation_status,
        safe_result,
    ):
        row = self.repository.row
        if row is None or row["status"] != expected_state:
            return None
        row["status"] = new_state
        row["result"] = dict(safe_result)
        row["completed_at"] = datetime.now(timezone.utc)
        self.repository.operation["execution_state"] = new_state
        return row


def _approval_service_with_fake_repository(monkeypatch):
    monkeypatch.setattr(
        "app.services.calendar_action_executor.settings.CALENDAR_CREATE_EVENT_ENABLED",
        True,
    )
    repository = _InMemoryApprovalRepository()
    service = ActionApprovalService(db=None, repository=repository)
    service.connection_repository = SimpleNamespace(
        get_active_google_connection=Mock(
            return_value=SimpleNamespace(email="owner@example.com"),
        ),
    )
    service.credential_repository = SimpleNamespace(
        get_by_connection=Mock(
            return_value=SimpleNamespace(
                scopes=["https://www.googleapis.com/auth/calendar.events"],
            ),
        ),
    )
    service.calendar_operation_repository = (
        _InMemoryCalendarOperationRepository(repository)
    )
    return service, repository


def _create_and_approve(service, parameters=None):
    payload = ApprovalProposalCreate.model_validate({
        "conversation_id": uuid4(),
        "source_message_id": uuid4(),
        "connection_id": CONNECTION_ID,
        "action_type": "calendar.create_event",
        "parameters": parameters or _create_event_payload(),
    })
    created = service.create_proposal(
        user_id=USER_ID,
        payload=payload,
        proposal_id=PROPOSAL_ID,
    )
    approved = service.approve(user_id=USER_ID, proposal_id=PROPOSAL_ID)
    return created, approved


def test_create_identity_parameters_keep_null_optional_fields():
    parameters = CalendarCreateEventParameters.model_validate(
        _create_event_payload()
    )
    canonical = _calendar_identity_parameters(parameters)

    assert canonical["recurrence"] is None
    assert canonical["reminders"] is None
    assert canonical == parameters.model_dump(mode="json")
    assert (
        _calendar_identity_parameters(
            CalendarCreateEventParameters.model_validate(canonical)
        )
        == canonical
    )


def test_create_proposal_approval_and_execution_share_identity_with_null_recurrence(
    monkeypatch,
):
    service, repository = _approval_service_with_fake_repository(monkeypatch)
    created, approved = _create_and_approve(service)

    # Proposal creation persists the canonical payload, including
    # recurrence=None. Rows written before this fix had this exact shape, so
    # their stored fingerprints remain valid after the executor correction.
    assert "recurrence" in created.parameters
    assert created.parameters["recurrence"] is None
    assert approved.status == "approved"
    assert approved.execution_available is True

    identity = build_calendar_operation_identity(
        proposal_id=PROPOSAL_ID,
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        action_type="calendar.create_event",
        parameters=created.parameters,
    )
    assert created.payload_fingerprint == identity.payload_fingerprint
    assert created.operation_id == identity.operation_id

    event = {
        "id": identity.event_id,
        "summary": created.parameters["summary"],
        "description": None,
        "location": None,
        "start": {
            "dateTime": created.parameters["start"],
            "timeZone": created.parameters["time_zone"],
        },
        "end": {
            "dateTime": created.parameters["end"],
            "timeZone": created.parameters["time_zone"],
        },
        "attendees": [],
        "extendedProperties": {
            "private": {
                "personal_ai_operation_id": str(identity.operation_id),
                "personal_ai_payload_fingerprint": (
                    identity.payload_fingerprint
                ),
            },
        },
    }
    calendar_service = SimpleNamespace(
        validate_create_event_access=AsyncMock(),
    )
    api = SimpleNamespace(request_mutation=AsyncMock(return_value={}))
    monkeypatch.setattr(
        "app.services.calendar_action_executor.SessionLocal",
        lambda: SimpleNamespace(close=Mock()),
    )
    executor = CalendarActionExecutor(
        db=None,
        approval_service=service,
        calendar_service=calendar_service,
        api_client=api,
    )
    executor._read_back = AsyncMock(return_value=event)

    result = asyncio.run(
        executor.execute(user_id=USER_ID, proposal_id=PROPOSAL_ID)
    )

    assert result.status == "completed"
    calendar_service.validate_create_event_access.assert_awaited_once()
    api.request_mutation.assert_awaited_once()
    request = api.request_mutation.call_args.kwargs
    assert request["method"] == "POST"
    assert request["json_data"]["summary"] == "Design review"
    assert "recurrence" not in request["json_data"]
    assert repository.operation["execution_state"] == "completed"


def test_pre_existing_approved_create_proposal_executes_after_normalization_fix(
    monkeypatch,
):
    service, repository = _approval_service_with_fake_repository(monkeypatch)
    parameters = CalendarCreateEventParameters.model_validate(
        _create_event_payload()
    ).model_dump(mode="json")
    identity = build_calendar_operation_identity(
        proposal_id=PROPOSAL_ID,
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        action_type="calendar.create_event",
        parameters=parameters,
    )
    now = datetime.now(timezone.utc)

    # Simulate an approved row written before this fix: the stored canonical
    # payload contains recurrence=None and no execution has happened yet.
    repository.operation = {
        "proposal_id": str(PROPOSAL_ID),
        "operation_id": str(identity.operation_id),
        "user_id": str(USER_ID),
        "connection_id": str(CONNECTION_ID),
        "action_type": "calendar.create_event",
        "calendar_id": parameters["calendar_id"],
        "event_id": identity.event_id,
        "payload_fingerprint": identity.payload_fingerprint,
        "execution_state": "approved",
    }
    repository.row = {
        "proposal_id": PROPOSAL_ID,
        "message_id": uuid4(),
        "source_message_id": uuid4(),
        "user_id": USER_ID,
        "conversation_id": uuid4(),
        "action_type": "calendar.create_event",
        "parameters": parameters,
        "connection_id": CONNECTION_ID,
        "account_email": "owner@example.com",
        "created_at": now,
        "expires_at": now + timedelta(minutes=15),
        "status": "approved",
        "approved_at": now,
        "execution_started_at": None,
        "completed_at": None,
        "result": None,
        "integrity_schema_version": 1,
        "payload_fingerprint": identity.payload_fingerprint,
        "operation_id": identity.operation_id,
        "calendar_event_id": identity.event_id,
    }

    event = {
        "id": identity.event_id,
        "summary": parameters["summary"],
        "description": None,
        "location": None,
        "start": {
            "dateTime": parameters["start"],
            "timeZone": parameters["time_zone"],
        },
        "end": {
            "dateTime": parameters["end"],
            "timeZone": parameters["time_zone"],
        },
        "attendees": [],
        "extendedProperties": {
            "private": {
                "personal_ai_operation_id": str(identity.operation_id),
                "personal_ai_payload_fingerprint": (
                    identity.payload_fingerprint
                ),
            },
        },
    }
    calendar_service = SimpleNamespace(
        validate_create_event_access=AsyncMock(),
    )
    api = SimpleNamespace(request_mutation=AsyncMock(return_value={}))
    monkeypatch.setattr(
        "app.services.calendar_action_executor.SessionLocal",
        lambda: SimpleNamespace(close=Mock()),
    )
    executor = CalendarActionExecutor(
        db=None,
        approval_service=service,
        calendar_service=calendar_service,
        api_client=api,
    )
    executor._read_back = AsyncMock(return_value=event)

    result = asyncio.run(
        executor.execute(user_id=USER_ID, proposal_id=PROPOSAL_ID)
    )

    assert result.status == "completed"
    api.request_mutation.assert_awaited_once()
    assert repository.operation["execution_state"] == "completed"


def test_tampered_approved_calendar_create_is_rejected_before_google_call(
    monkeypatch,
):
    service, repository = _approval_service_with_fake_repository(monkeypatch)
    _create_and_approve(service)

    repository.row["parameters"]["summary"] = "Tampered meeting"

    api = SimpleNamespace(request_mutation=AsyncMock())
    executor = CalendarActionExecutor(
        db=None,
        approval_service=service,
        calendar_service=SimpleNamespace(),
        api_client=api,
    )

    with pytest.raises(
        ApprovalConflictError,
        match="integrity verification failed",
    ):
        asyncio.run(
            executor.execute(user_id=USER_ID, proposal_id=PROPOSAL_ID)
        )

    api.request_mutation.assert_not_awaited()
    assert repository.operation["execution_state"] == "approved"


def test_revised_calendar_create_payload_has_a_new_valid_fingerprint(monkeypatch):
    original = _create_event_payload()
    revised = _create_event_payload(
        start="2026-06-10T21:00:00+05:30",
        end="2026-06-10T22:00:00+05:30",
        time_zone="Asia/Kolkata",
    )
    original_identity = build_calendar_operation_identity(
        proposal_id=PROPOSAL_ID,
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        action_type="calendar.create_event",
        parameters=_calendar_identity_parameters(
            CalendarCreateEventParameters.model_validate(original)
        ),
    )
    revised_identity = build_calendar_operation_identity(
        proposal_id=uuid4(),
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        action_type="calendar.create_event",
        parameters=_calendar_identity_parameters(
            CalendarCreateEventParameters.model_validate(revised)
        ),
    )
    assert (
        revised_identity.payload_fingerprint
        != original_identity.payload_fingerprint
    )

    service, _repository = _approval_service_with_fake_repository(monkeypatch)
    created, approved = _create_and_approve(service, parameters=revised)

    assert approved.status == "approved"
    assert created.payload_fingerprint == revised_identity.payload_fingerprint
    assert created.payload_fingerprint != original_identity.payload_fingerprint


def test_old_approval_cannot_authorize_a_changed_calendar_time_payload(
    monkeypatch,
):
    service, repository = _approval_service_with_fake_repository(monkeypatch)
    _create_and_approve(service)

    repository.row["parameters"]["start"] = "2026-06-10T21:00:00+05:30"
    repository.row["parameters"]["end"] = "2026-06-10T22:00:00+05:30"

    api = SimpleNamespace(request_mutation=AsyncMock())
    executor = CalendarActionExecutor(
        db=None,
        approval_service=service,
        calendar_service=SimpleNamespace(),
        api_client=api,
    )

    with pytest.raises(
        ApprovalConflictError,
        match="integrity verification failed",
    ):
        asyncio.run(
            executor.execute(user_id=USER_ID, proposal_id=PROPOSAL_ID)
        )

    api.request_mutation.assert_not_awaited()
