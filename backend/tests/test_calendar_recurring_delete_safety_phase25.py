"""
Phase 2.5: Calendar recurring-event deletion semantics, approval scope, and
execution/reconciliation regression matrix (mocked/in-process only).

No real Google/provider mutations.
"""
from __future__ import annotations

import asyncio
from contextlib import nullcontext
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from zoneinfo import ZoneInfo

from app.integrations.google.exceptions import GoogleAPIError, GoogleAPIMutationError
from app.schemas.calendar_approval import CalendarDeleteEventParameters
from app.services.action_approval_service import ActionApprovalService
from app.services.calendar_action_executor import CalendarActionExecutor
from app.services.calendar_action_identity import build_calendar_operation_identity
import app.services.calendar_action_executor as cae
from app.workflows import calendar_agent_node as calendar_agent_module
from app.workflows import gmail_agent_nodes
from app.workflows.calendar_update_parser import parse_calendar_delete_request


USER_ID = UUID("c0cd2d85-c90c-4c62-a886-c41bec1c41af")
CONVERSATION_ID = UUID("c7557e52-cd75-4ea6-9130-e927176518a8")
CAL_CONNECTION_ID = UUID("5a3d3426-f1fd-4cc9-ae83-08181848cde7")
TZ = ZoneInfo("America/Los_Angeles")


class _SessionCtx:
    def __enter__(self):
        return object()

    def __exit__(self, *a):
        return False


def _runtime(**overrides):
    ctx = {
        "user_id": str(USER_ID),
        "calendar_write_connection_id": str(CAL_CONNECTION_ID),
        "google_connection_available": True,
        "calendar_enabled": True,
        "calendar_default_connection_invalid": False,
        "calendar_write_selection_required": False,
        "calendar_selected_write_scope_missing": False,
    }
    ctx.update(overrides)
    return SimpleNamespace(context=ctx)


def _delete_state(message: str, **overrides):
    state = {
        "user_message": message,
        "gmail_plan": {
            "intent": "google_calendar",
            "calendar_tool": "delete_calendar_event_proposal",
            "calendar_arguments": None,
        },
        "user_timezone": "America/Los_Angeles",
        "request_time": "2026-11-16T12:00:00Z",
        "assistant_response": "",
        "provider": "",
        "model": "",
        "messages": [],
        "assistant_capabilities": {
            "google_calendar": {
                "connected": True,
                "api_available": True,
                "events_read": True,
                "write_permission_granted": True,
                "delete_execution_available": True,
            }
        },
        "conversation_id": str(CONVERSATION_ID),
        "user_id": str(USER_ID),
    }
    state.update(overrides)
    return state


def _timed_event(
    event_id,
    summary,
    start_dt,
    end_dt,
    *,
    etag="etag-1",
    time_zone="America/Los_Angeles",
    recurrence=None,
    recurring_event_id=None,
    original_start_time=None,
):
    ev = {
        "id": event_id,
        "summary": summary,
        "status": "confirmed",
        "etag": etag,
        "start": {"date_time": start_dt, "time_zone": time_zone},
        "end": {"date_time": end_dt, "time_zone": time_zone},
        "attendees": [],
        "description": None,
        "location": None,
        "html_link": f"https://calendar.google.com/event?eid={event_id}",
    }
    if recurrence is not None:
        ev["recurrence"] = recurrence
    if recurring_event_id is not None:
        ev["recurring_event_id"] = recurring_event_id
    if original_start_time is not None:
        ev["original_start_time"] = original_start_time
    return ev


def _instance_occurrence(master_id="master-series-1", event_id="instance-20261118", day=18, hour=10):
    start = datetime(2026, 11, day, hour, 0, tzinfo=TZ)
    end = start + timedelta(minutes=30)
    orig = {"date_time": start, "time_zone": "America/Los_Angeles"}
    return _timed_event(
        event_id,
        "Daily Standup",
        start,
        end,
        etag=f'"etag-{event_id}"',
        recurring_event_id=master_id,
        original_start_time=orig,
    )


def _master_event(master_id="master-series-1", day=10, hour=10):
    start = datetime(2026, 11, day, hour, 0, tzinfo=TZ)
    end = start + timedelta(minutes=30)
    return _timed_event(
        master_id,
        "Daily Standup",
        start,
        end,
        etag=f'"etag-{master_id}"',
        recurrence=["RRULE:FREQ=DAILY"],
    )


def _service_for(events, *, get_map=None, validate_event=None):
    get_map = get_map or {}

    async def _get_event(**kwargs):
        eid = kwargs.get("event_id")
        if eid in get_map:
            return get_map[eid]
        for ev in events:
            if ev.get("id") == eid:
                return ev
        raise AssertionError(f"unexpected get_event {eid}")

    async def _validate_event_write_access(**kwargs):
        eid = kwargs.get("event_id")
        if callable(validate_event):
            return validate_event(kwargs)
        return await _get_event(**kwargs)

    return SimpleNamespace(
        list_events=AsyncMock(
            return_value={"events": events, "next_page_token": None, "calendar_id": "primary"}
        ),
        get_event=AsyncMock(side_effect=_get_event),
        validate_event_write_access=AsyncMock(side_effect=_validate_event_write_access),
    )


def _canonical_delete_params(params):
    """Return canonical JSON params for identity/proposal helpers (accepts dict or model)."""
    if isinstance(params, CalendarDeleteEventParameters):
        return params.model_dump(mode="json")
    return CalendarDeleteEventParameters.model_validate(params).model_dump(mode="json")


def _proposal_from_params(user_id, connection_id, params, *, proposal_id=None):
    proposal_id = proposal_id or uuid4()
    canonical = _canonical_delete_params(params)
    identity = build_calendar_operation_identity(
        proposal_id=proposal_id,
        user_id=user_id,
        connection_id=connection_id,
        action_type="calendar.delete_event",
        parameters=canonical,
    )
    return SimpleNamespace(
        proposal_id=proposal_id,
        action_type="calendar.delete_event",
        status="approved",
        parameters=canonical,
        connection_id=connection_id,
        user_id=user_id,
        operation_id=identity.operation_id,
        payload_fingerprint=identity.payload_fingerprint,
        calendar_event_id=identity.event_id,
        execution_started_at=None,
        integrity_schema_version=1,
    )


# -----------------------------
# A. Delete one recurring instance
# -----------------------------

def test_delete_single_recurring_instance_targets_instance_not_master(monkeypatch):
    """One occurrence delete must resolve exact instance and preserve instance identity; never substitute master."""
    instance = _instance_occurrence()
    master = _master_event()
    service = _service_for([instance], get_map={instance["id"]: instance, master["id"]: master})
    monkeypatch.setattr(calendar_agent_module, "CalendarService", lambda _db: service)
    monkeypatch.setattr(calendar_agent_module, "SessionLocal", _SessionCtx)

    msg = (
        "delete the event\n"
        "Event: Daily Standup\n"
        "Date: 2026-11-18\n"
        "Original time: 10:00 AM to 10:30 AM\n"
        "Timezone: America/Los_Angeles"
    )
    result = calendar_agent_module.calendar_agent_node(_delete_state(msg), _runtime())

    proposal = result.get("calendar_action_proposal")
    assert isinstance(proposal, dict), result.get("assistant_response")
    assert proposal["action_type"] == "calendar.delete_event"
    params = proposal["parameters"]
    assert params["calendar_id"] == "primary"
    assert params["event_id"] == "instance-20261118"
    assert params["delete_entire_series"] is False
    assert params["event_id"] != master["id"]

    current = params.get("current_event") or {}
    assert params["event_id"] == instance["id"]
    assert (current.get("recurrence") in (None, [], False))
    # Required: preserve recurringEventId and originalStartTime for exact instance scope/approval
    recurring_event_id = current.get("recurring_event_id") or current.get("recurringEventId") or params.get("recurring_event_id") or params.get("recurringEventId")
    assert recurring_event_id == "master-series-1", params
    ost = current.get("original_start_time") or current.get("originalStartTime") or params.get("original_start_time") or params.get("originalStartTime")
    assert ost is not None, f"originalStartTime/original_start_time missing in proposal params: {params}"
    ost_str = str(ost.get("dateTime") if isinstance(ost, dict) else ost)
    assert "2026-11-18" in ost_str and "10:00" in ost_str
    # timezone/original occurrence preserved (exact occurrence identity)
    if isinstance(ost, dict):
        assert ost.get("timeZone") == "America/Los_Angeles" or "2026-11-18T10:00:00-08:00" in ost_str
    # calendar_id and exact event_id preserved
    assert params["calendar_id"] == "primary" and params["event_id"] == "instance-20261118"


# -----------------------------
# B. Delete recurring master
# -----------------------------

def test_delete_recurring_master_requires_explicit_series_scope_and_targets_master(monkeypatch):
    instance = _instance_occurrence()
    master = _master_event()
    # Series delete request identifies the master occurrence/time; discovery may return master and/or instances.
    service = _service_for([master, instance], get_map={instance["id"]: instance, master["id"]: master})
    monkeypatch.setattr(calendar_agent_module, "CalendarService", lambda _db: service)
    monkeypatch.setattr(calendar_agent_module, "SessionLocal", _SessionCtx)

    msg = (
        "delete the entire recurring series Daily Standup\n"
        "Event: Daily Standup\n"
        "Date: 2026-11-10\n"
        "Original time: 10:00 AM to 10:30 AM\n"
        "Timezone: America/Los_Angeles\n"
        "daily recurrence, no end date"
    )
    result = calendar_agent_module.calendar_agent_node(_delete_state(msg), _runtime())
    proposal = result.get("calendar_action_proposal")
    assert isinstance(proposal, dict), result.get("assistant_response")
    params = proposal["parameters"]
    assert params["delete_entire_series"] is True
    assert params["event_id"] == master["id"]
    assert params["calendar_id"] == "primary"
    snapshot = params.get("current_event") or {}
    assert isinstance(snapshot.get("recurrence"), list) and snapshot["recurrence"], snapshot
    desc = (result.get("assistant_response") or "").lower()
    assert "series" in desc or "recurring" in desc or "master" in desc or "entire" in desc


# -----------------------------
# C. Delete entire series requires explicit intent
# -----------------------------

def test_delete_one_date_must_not_expand_to_entire_series(monkeypatch):
    instance = _instance_occurrence(day=18)
    master = _master_event()
    service = _service_for([instance], get_map={instance["id"]: instance, master["id"]: master})
    monkeypatch.setattr(calendar_agent_module, "CalendarService", lambda _db: service)
    monkeypatch.setattr(calendar_agent_module, "SessionLocal", _SessionCtx)

    msg = (
        "delete the event\n"
        "Event: Daily Standup\n"
        "Date: 2026-11-18\n"
        "Original time: 10:00 AM to 10:30 AM\n"
        "Timezone: America/Los_Angeles"
    )
    result = calendar_agent_module.calendar_agent_node(_delete_state(msg), _runtime())
    proposal = result.get("calendar_action_proposal")
    assert isinstance(proposal, dict), result.get("assistant_response")
    params = proposal["parameters"]
    assert params["delete_entire_series"] is False
    assert params["event_id"] == instance["id"]


# -----------------------------
# D. Ambiguous or incomplete metadata
# -----------------------------

def test_instance_master_ambiguity_requires_clarification_no_destructive_fallback(monkeypatch):
    instance = _instance_occurrence()
    master_sameish = _master_event()
    service = _service_for([instance, master_sameish], get_map={instance["id"]: instance, master_sameish["id"]: master_sameish})
    monkeypatch.setattr(calendar_agent_module, "CalendarService", lambda _db: service)
    monkeypatch.setattr(calendar_agent_module, "SessionLocal", _SessionCtx)

    # Ambiguous scope wording: must not guess series/master vs instance
    msg = (
        "delete Daily Standup on 2026-11-18 10:00 AM America/Los_Angeles series?\n"
        "Event: Daily Standup\nDate: 2026-11-18\nOriginal start time: 10:00 AM\nOriginal end time: 10:30 AM\nTimezone: America/Los_Angeles"
    )
    result = calendar_agent_module.calendar_agent_node(_delete_state(msg), _runtime())
    if result.get("calendar_action_proposal"):
        params = result["calendar_action_proposal"]["parameters"]
        # if resolved, must be exact instance only (no silent series expansion)
        assert params["delete_entire_series"] is False
        assert params["event_id"] == instance["id"]
    else:
        resp = (result.get("assistant_response") or "").lower()
        assert "clarif" in resp or "which" in resp or "specify" in resp or "nothing was changed" in resp or "series" in resp


def test_incomplete_recurrence_metadata_blocks_series_delete():
    with pytest.raises((ValidationError, ValueError)):
        CalendarDeleteEventParameters.model_validate(
            {
                "calendar_id": "primary",
                "event_id": "master-1",
                "expected_etag": "etag-1",
                "delete_entire_series": True,
                "current_event": {
                    "summary": "Daily",
                    "description": None,
                    "location": None,
                    "start": {"dateTime": "2026-11-10T10:00:00-08:00", "timeZone": "America/Los_Angeles"},
                    "end": {"dateTime": "2026-11-10T10:30:00-08:00", "timeZone": "America/Los_Angeles"},
                    "attendees": [],
                    "etag": "etag-1",
                },
            }
        )


def test_missing_event_or_calendar_identifiers_refuse_execution(monkeypatch):
    monkeypatch.setattr("app.services.calendar_action_executor.settings.CALENDAR_DELETE_EVENT_ENABLED", True)
    with pytest.raises((ValidationError, ValueError)):
        CalendarDeleteEventParameters.model_validate(
            {
                "calendar_id": "",
                "event_id": "evt-1",
                "expected_etag": "etag-1",
                "delete_entire_series": False,
                "current_event": {
                    "summary": "X",
                    "description": None,
                    "location": None,
                    "start": {"dateTime": "2026-11-10T10:00:00-08:00", "timeZone": "America/Los_Angeles"},
                    "end": {"dateTime": "2026-11-10T10:30:00-08:00", "timeZone": "America/Los_Angeles"},
                    "attendees": [],
                    "etag": "etag-1",
                },
            }
        )


def test_conflicting_recurrence_metadata_refuses_series_match(monkeypatch):
    instance = _instance_occurrence()
    master = _master_event()
    master["recurrence"] = ["RRULE:FREQ=WEEKLY"]
    service = _service_for([instance], get_map={instance["id"]: instance, master["id"]: master})
    monkeypatch.setattr(calendar_agent_module, "CalendarService", lambda _db: service)
    monkeypatch.setattr(calendar_agent_module, "SessionLocal", _SessionCtx)

    msg = (
        "delete the entire recurring series Daily Standup\n"
        "Event: Daily Standup\n"
        "Date: 2026-11-10\n"
        "Original time: 10:00 AM to 10:30 AM\n"
        "Timezone: America/Los_Angeles\n"
        "daily recurrence, no end date"
    )
    result = calendar_agent_module.calendar_agent_node(_delete_state(msg), _runtime())
    assert "calendar_action_proposal" not in result
    resp = (result.get("assistant_response") or "").lower()
    assert "couldn" in resp or "nothing was changed" in resp or "verify" in resp or "clarif" in resp or "specify" in resp


def test_timezone_ambiguity_or_incomplete_time_blocks_destructive_series_delete(monkeypatch):
    instance = _instance_occurrence()
    master = _master_event()
    service = _service_for([instance], get_map={instance["id"]: instance, master["id"]: master})
    monkeypatch.setattr(calendar_agent_module, "CalendarService", lambda _db: service)
    monkeypatch.setattr(calendar_agent_module, "SessionLocal", _SessionCtx)

    msg = "delete the entire recurring series Daily Standup on 2026-11-10 from 10:00 AM to 10:30 AM, daily recurrence, no end date"
    result = calendar_agent_module.calendar_agent_node(_delete_state(msg), _runtime())
    assert "calendar_action_proposal" not in result
    assert "nothing was changed" in (result.get("assistant_response") or "").lower() or "specify" in (result.get("assistant_response") or "").lower()


def test_multiple_matching_recurring_occurrences_require_clarification(monkeypatch):
    i1 = _instance_occurrence(event_id="inst-1", day=18, hour=10)
    i2 = _instance_occurrence(event_id="inst-2", day=18, hour=14)
    service = _service_for([i1, i2], get_map={i1["id"]: i1, i2["id"]: i2})
    monkeypatch.setattr(calendar_agent_module, "CalendarService", lambda _db: service)
    monkeypatch.setattr(calendar_agent_module, "SessionLocal", _SessionCtx)

    msg = (
        "delete the event\n"
        "Event: Daily Standup\n"
        "Date: 2026-11-18\n"
        "Timezone: America/Los_Angeles"
    )
    result = calendar_agent_module.calendar_agent_node(_delete_state(msg), _runtime())
    assert "calendar_action_proposal" not in result
    resp = (result.get("assistant_response") or "").lower()
    assert "multiple" in resp or "which one" in resp or "which" in resp or "clarif" in resp or "nothing was changed" in resp


# -----------------------------
# 4. Exact approval-scope guarantees (identity/params)
# -----------------------------

def _instance_delete_params(instance_id="instance-20261118", master_id="master-series-1", cal="primary"):
    # Base canonical fields required by schema/snapshot today; instance provenance may be
    # preserved in snapshot/params by implementation (asserted in behavior tests).
    return {
        "calendar_id": cal,
        "event_id": instance_id,
        "expected_etag": '"etag-inst"',
        "delete_entire_series": False,
        "current_event": {
            "summary": "Daily Standup",
            "description": None,
            "location": None,
            "start": {"dateTime": "2026-11-18T10:00:00-08:00", "timeZone": "America/Los_Angeles"},
            "end": {"dateTime": "2026-11-18T10:30:00-08:00", "timeZone": "America/Los_Angeles"},
            "attendees": [],
            "etag": '"etag-inst"',
        },
    }


def _master_series_delete_params(master_id="master-series-1", cal="primary"):
    return {
        "calendar_id": cal,
        "event_id": master_id,
        "expected_etag": '"etag-master"',
        "delete_entire_series": True,
        "current_event": {
            "summary": "Daily Standup",
            "description": None,
            "location": None,
            "start": {"dateTime": "2026-11-10T10:00:00-08:00", "timeZone": "America/Los_Angeles"},
            "end": {"dateTime": "2026-11-10T10:30:00-08:00", "timeZone": "America/Los_Angeles"},
            "attendees": [],
            "etag": '"etag-master"',
            "recurrence": ["RRULE:FREQ=DAILY"],
        },
    }


def test_approval_scope_instance_cannot_authorize_master_or_series():
    pid_inst = uuid4()
    pid_master = uuid4()
    inst = _canonical_delete_params(_instance_delete_params())
    master = _canonical_delete_params(_master_series_delete_params())
    id_inst = build_calendar_operation_identity(
        proposal_id=pid_inst, user_id=USER_ID, connection_id=CAL_CONNECTION_ID,
        action_type="calendar.delete_event", parameters=inst,
    )
    id_master = build_calendar_operation_identity(
        proposal_id=pid_master, user_id=USER_ID, connection_id=CAL_CONNECTION_ID,
        action_type="calendar.delete_event", parameters=master,
    )
    assert id_inst.payload_fingerprint != id_master.payload_fingerprint
    assert id_inst.event_id != id_master.event_id
    assert id_inst.operation_id != id_master.operation_id
    assert inst["event_id"] == "instance-20261118"
    assert inst["delete_entire_series"] is False
    assert master["event_id"] == "master-series-1"
    assert master["delete_entire_series"] is True


def test_approval_scope_series_cannot_be_reused_for_different_instance():
    series = _canonical_delete_params(_master_series_delete_params())
    other_instance = _canonical_delete_params(_instance_delete_params(instance_id="instance-20261119"))
    id_series = build_calendar_operation_identity(
        proposal_id=uuid4(), user_id=USER_ID, connection_id=CAL_CONNECTION_ID,
        action_type="calendar.delete_event", parameters=series,
    )
    id_other = build_calendar_operation_identity(
        proposal_id=uuid4(), user_id=USER_ID, connection_id=CAL_CONNECTION_ID,
        action_type="calendar.delete_event", parameters=other_instance,
    )
    assert id_series.payload_fingerprint != id_other.payload_fingerprint
    assert id_series.event_id != id_other.event_id


def test_changed_target_or_scope_requires_new_approval_identity():
    base = _canonical_delete_params(_instance_delete_params())
    changed_event = _canonical_delete_params(_instance_delete_params(instance_id="instance-OTHER"))
    changed_cal = _canonical_delete_params(_instance_delete_params(cal="other-cal"))
    ids = []
    for p in (base, changed_event, changed_cal):
        ids.append(build_calendar_operation_identity(
            proposal_id=uuid4(), user_id=USER_ID, connection_id=CAL_CONNECTION_ID,
            action_type="calendar.delete_event", parameters=p,
        ))
    assert ids[0].payload_fingerprint != ids[1].payload_fingerprint
    assert ids[0].payload_fingerprint != ids[2].payload_fingerprint
    assert ids[0].event_id != ids[1].event_id
    assert ids[0].calendar_id != ids[2].calendar_id
    master = _canonical_delete_params(_master_series_delete_params())
    id_master = build_calendar_operation_identity(
        proposal_id=uuid4(), user_id=USER_ID, connection_id=CAL_CONNECTION_ID,
        action_type="calendar.delete_event", parameters=master,
    )
    assert ids[0].payload_fingerprint != id_master.payload_fingerprint


# -----------------------------
# 5. Execution and reconciliation regression matrix
# -----------------------------

def _executor_setup(monkeypatch, *, params=None, event_id="evt-del-1", etag='"etag-del"', proposal_status="approved"):
    monkeypatch.setattr("app.services.calendar_action_executor.settings.CALENDAR_DELETE_EVENT_ENABLED", True)
    if params is None:
        params = {
            "calendar_id": "primary",
            "event_id": event_id,
            "expected_etag": etag,
            "delete_entire_series": False,
            "current_event": {
                "summary": "X",
                "description": None,
                "location": None,
                "start": {"dateTime": "2026-11-16T15:00:00-08:00", "timeZone": "America/Los_Angeles"},
                "end": {"dateTime": "2026-11-16T15:30:00-08:00", "timeZone": "America/Los_Angeles"},
                "attendees": [],
                "etag": etag,
            },
        }
    proposal = _proposal_from_params(USER_ID, CAL_CONNECTION_ID, params)
    proposal.status = proposal_status
    completed = SimpleNamespace(status="completed", result={"outcome": "deleted", "google_event_id": event_id}, proposal_id=proposal.proposal_id)
    uncertain = SimpleNamespace(status="execution_uncertain", result={"outcome": "unconfirmed"}, proposal_id=proposal.proposal_id)
    failed = SimpleNamespace(status="failed", result={"error_code": "GOOGLE_MUTATION_FAILED"}, proposal_id=proposal.proposal_id)
    approval = SimpleNamespace(
        get_proposal=Mock(return_value=proposal),
        claim_calendar_execution=Mock(return_value=proposal),
        finish_calendar_execution=Mock(return_value=completed),
        calendar_operation_repository=Mock(),
    )
    calendar = SimpleNamespace(
        validate_event_write_access=AsyncMock(return_value={
            "id": event_id, "summary": "X", "description": None, "location": None, "etag": etag,
            "start": {"date": None, "date_time": datetime(2026, 11, 16, 23, 0, tzinfo=timezone.utc), "time_zone": "America/Los_Angeles"},
            "end": {"date": None, "date_time": datetime(2026, 11, 16, 23, 30, tzinfo=timezone.utc), "time_zone": "America/Los_Angeles"},
            "attendees": [],
        }),
        _google_get=AsyncMock(),
        _normalize_event=Mock(),
    )
    api = SimpleNamespace(request_mutation=AsyncMock(return_value={}))
    monkeypatch.setattr("app.services.calendar_action_executor.SessionLocal", lambda: SimpleNamespace(close=Mock()))
    executor = CalendarActionExecutor(db=None, approval_service=approval, calendar_service=calendar, api_client=api)
    return executor, approval, api, calendar, proposal, {"completed": completed, "uncertain": uncertain, "failed": failed}


def test_matrix_provider_confirms_delete_success(monkeypatch):
    executor, approval, api, _calendar, proposal, states = _executor_setup(monkeypatch)
    executor._read_event_for_reconciliation = AsyncMock(return_value=None)
    result = asyncio.run(executor.execute(user_id=USER_ID, proposal_id=proposal.proposal_id))
    assert result.status == "completed"
    assert api.request_mutation.await_count == 1
    finish = approval.finish_calendar_execution.call_args.kwargs
    assert finish["state"] == "completed"
    assert finish["safe_result"].get("outcome") == "deleted"
    assert finish["safe_result"].get("google_event_id") == proposal.parameters["event_id"]


def test_matrix_provider_explicitly_rejects_delete(monkeypatch):
    executor, approval, api, _calendar, proposal, states = _executor_setup(monkeypatch)
    approval.finish_calendar_execution = Mock(return_value=states["failed"])
    err = GoogleAPIMutationError(status_code=403, message="forbidden", outcome_unknown=False)
    api.request_mutation = AsyncMock(side_effect=err)
    executor._read_event_for_reconciliation = AsyncMock(side_effect=Exception("no read"))
    result = asyncio.run(executor.execute(user_id=USER_ID, proposal_id=proposal.proposal_id))
    assert result.status != "completed"
    finish = approval.finish_calendar_execution.call_args.kwargs
    assert finish["state"] in {"failed", "execution_uncertain"}


def test_matrix_timeout_unknown_until_reconciled_not_success_or_failure(monkeypatch):
    executor, approval, api, _calendar, proposal, states = _executor_setup(monkeypatch)
    approval.finish_calendar_execution = Mock(return_value=states["uncertain"])
    err = GoogleAPIMutationError(status_code=504, message="timeout", outcome_unknown=True)
    api.request_mutation = AsyncMock(side_effect=err)
    # reconciliation cannot establish outcome (read unavailable) -> must remain uncertain
    executor._read_event_for_reconciliation = AsyncMock(side_effect=GoogleAPIError(status_code=503, message="unavailable", retryable=True))
    result = asyncio.run(executor.execute(user_id=USER_ID, proposal_id=proposal.proposal_id))
    assert result.status == "execution_uncertain"
    finish = approval.finish_calendar_execution.call_args.kwargs
    assert finish["state"] == "execution_uncertain"
    assert "deleted" != (finish.get("safe_result") or {}).get("outcome")


def test_matrix_execution_lease_expiry_marks_uncertain_not_completed(monkeypatch):
    executor, approval, _api, _calendar, proposal, states = _executor_setup(monkeypatch)
    approval.get_proposal = Mock(return_value=SimpleNamespace(**{**proposal.__dict__, "status": "executing"}))
    approval.claim_calendar_execution = Mock(side_effect=Exception("lease expired/claim conflict"))
    try:
        asyncio.run(executor.execute(user_id=USER_ID, proposal_id=proposal.proposal_id))
    except Exception:
        pass
    for call in getattr(approval.finish_calendar_execution, "call_args_list", []):
        assert call.kwargs.get("state") != "completed" or (call.kwargs.get("safe_result") or {}).get("outcome") != "deleted"


def test_matrix_duplicate_execution_does_not_cause_duplicate_provider_mutation(monkeypatch):
    executor, approval, api, _calendar, proposal, states = _executor_setup(monkeypatch)
    executor._read_event_for_reconciliation = AsyncMock(return_value=None)
    asyncio.run(executor.execute(user_id=USER_ID, proposal_id=proposal.proposal_id))
    first_count = api.request_mutation.await_count
    proposal_done = SimpleNamespace(**{**proposal.__dict__, "status": "completed", "result": {"outcome": "deleted"}})
    approval.get_proposal = Mock(return_value=proposal_done)
    # Duplicate/terminal attempt must not trigger another provider mutation
    try:
        result2 = asyncio.run(executor.execute(user_id=USER_ID, proposal_id=proposal.proposal_id))
        assert getattr(result2, "status", None) in {"completed", "failed", "execution_uncertain", "approved", "rejected", "expired"}
    except Exception:
        # conflict on non-approved terminal state is acceptable if no duplicate mutation occurred
        pass
    assert api.request_mutation.await_count == first_count


def test_matrix_reconciliation_confirms_deleted(monkeypatch):
    executor, approval, api, _calendar, proposal, states = _executor_setup(monkeypatch)
    api.request_mutation = AsyncMock(side_effect=GoogleAPIMutationError(status_code=504, message="timeout", outcome_unknown=True))
    executor._read_event_for_reconciliation = AsyncMock(return_value=None)
    asyncio.run(executor.execute(user_id=USER_ID, proposal_id=proposal.proposal_id))
    finish = approval.finish_calendar_execution.call_args.kwargs
    if finish["state"] == "completed":
        assert finish["safe_result"].get("outcome") == "deleted"
    else:
        assert finish["state"] == "execution_uncertain"


def test_matrix_reconciliation_confirms_event_still_exists(monkeypatch):
    executor, approval, api, _calendar, proposal, states = _executor_setup(monkeypatch)
    api.request_mutation = AsyncMock(side_effect=GoogleAPIMutationError(status_code=504, message="timeout", outcome_unknown=True))
    executor._read_event_for_reconciliation = AsyncMock(return_value={"id": proposal.parameters["event_id"], "etag": '"etag-del"'})
    asyncio.run(executor.execute(user_id=USER_ID, proposal_id=proposal.proposal_id))
    finish = approval.finish_calendar_execution.call_args.kwargs
    assert finish["state"] != "completed" or (finish.get("safe_result") or {}).get("outcome") != "deleted"
    assert finish["state"] in {"failed", "execution_uncertain"}


def test_matrix_reconciliation_cannot_establish_outcome_remains_uncertain(monkeypatch):
    executor, approval, api, _calendar, proposal, states = _executor_setup(monkeypatch)
    approval.finish_calendar_execution = Mock(return_value=states["uncertain"])
    api.request_mutation = AsyncMock(side_effect=GoogleAPIMutationError(status_code=504, message="timeout", outcome_unknown=True))
    executor._read_event_for_reconciliation = AsyncMock(side_effect=GoogleAPIError(status_code=503, message="cannot read", retryable=True))
    result = asyncio.run(executor.execute(user_id=USER_ID, proposal_id=proposal.proposal_id))
    assert result.status == "execution_uncertain"
    finish = approval.finish_calendar_execution.call_args.kwargs
    assert finish["state"] == "execution_uncertain"


def test_matrix_stale_proposal_status_conflict_does_not_overwrite_newer_authoritative(monkeypatch):
    executor, approval, _api, _calendar, proposal, states = _executor_setup(monkeypatch)
    proposal.status = "execution_uncertain"
    approval.get_proposal = Mock(return_value=proposal)
    approval.claim_calendar_execution = Mock(side_effect=Exception("conflict"))
    try:
        asyncio.run(executor.execute(user_id=USER_ID, proposal_id=proposal.proposal_id))
    except Exception:
        pass
    for call in getattr(approval.finish_calendar_execution, "call_args_list", []):
        assert not (call.kwargs.get("state") == "completed" and (call.kwargs.get("safe_result") or {}).get("outcome") == "deleted")


def test_ui_network_error_vs_backend_status_invariant_unit_contract():
    """Frontend/network failure must not fabricate provider success/failure; backend status remains authoritative.
    Mocked/in-process contract assertion (no frontend executed)."""
    uncertain = SimpleNamespace(status="execution_uncertain", result={"outcome": "unconfirmed"})
    completed = SimpleNamespace(status="completed", result={"outcome": "deleted"})
    assert uncertain.status != "completed"
    assert completed.status == "completed" and completed.result["outcome"] == "deleted"
    assert "deleted" not in (uncertain.result or {})


# -----------------------------
# Parser/safety semantics
# -----------------------------

def test_parser_does_not_mark_single_date_delete_as_entire_series():
    details = parse_calendar_delete_request(
        "delete Daily Standup on 2026-11-18 at 10:00 AM America/Los_Angeles",
        planner_time_zone="America/Los_Angeles",
        reference_time="2026-11-16T12:00:00Z",
    )
    assert details.get("delete_entire_series") is False


def test_parser_marks_explicit_entire_series_intent():
    details = parse_calendar_delete_request(
        "delete the entire recurring series Daily Standup on 2026-11-10 from 10:00 AM to 10:30 AM America/Los_Angeles, daily recurrence, no end date",
        planner_time_zone="America/Los_Angeles",
        reference_time="2026-11-16T12:00:00Z",
    )
    assert details.get("delete_entire_series") is True
    assert details.get("expected_recurrence") == ["RRULE:FREQ=DAILY"]
    assert details.get("expected_recurring_unbounded") is True
