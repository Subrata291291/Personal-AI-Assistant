from contextlib import nullcontext
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest

from app.core.request_metrics import RequestMetrics, current_request_metrics
from app.services import chat_service as chat_service_module
from app.services.chat_service import ChatService
from app.workflows import calendar_agent_node as calendar_agent_module
from app.workflows import gmail_agent_nodes
from app.workflows.calendar_update_parser import parse_calendar_delete_request

USER_ID = UUID("c0cd2d85-c90c-4c62-a886-c41bec1c41af")
CONVERSATION_ID = UUID("c7557e52-cd75-4ea6-9130-e927176518a8")
CONNECTION_ID = UUID("5a3d3426-f1fd-4cc9-ae83-08181848cde7")
OTHER_CONNECTION_ID = UUID("99999999-9999-9999-9999-999999999999")
TZ_NAME = "Asia/Kolkata"
TZ = ZoneInfo(TZ_NAME)
REQUEST_TIME = "2026-11-14T15:00:00+05:30"


@pytest.fixture(autouse=True)
def _disable_llm_planner(monkeypatch):
    monkeypatch.setattr(
        gmail_agent_nodes.llm_router,
        "generate",
        Mock(side_effect=RuntimeError("planner disabled")),
    )

STANDALONE_DELETE_PHRASES = (
    "Delete the gym time event from November 15, 2026.",
    "delete the event from 15th november gym time",
    "delete gym time for from Nov 15",
    "Delete Gym time for on November 15",
    "Remove my gym event on November 15 at 9 AM",
)
TITLE_FOLLOWUP = "gym time for"
DATE_TIME_FOLLOWUP = (
    "gym time for from Nov 15, 2026, 9:00 AM – Nov 15, 2026, 10:00 AM"
)


def _runtime(**overrides):
    context = {
        "user_id": str(USER_ID),
        "calendar_write_connection_id": str(CONNECTION_ID),
        "google_connection_available": True,
        "calendar_enabled": True,
        "contacts_connection_id": None,
        "drive_connection_id": None,
    }
    context.update(overrides)
    return SimpleNamespace(context=context)


def _state(message, **overrides):
    state = {
        "user_message": message,
        "gmail_plan": {
            "intent": "google_calendar",
            "calendar_tool": "delete_calendar_event_proposal",
            "calendar_arguments": None,
        },
        "user_id": str(USER_ID),
        "conversation_id": str(CONVERSATION_ID),
        "user_timezone": TZ_NAME,
        "request_time": REQUEST_TIME,
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
    }
    state.update(overrides)
    return state


def _event(
    event_id,
    summary="Gym time for",
    start="2026-11-15T09:00:00+05:30",
    end="2026-11-15T10:00:00+05:30",
):
    return {
        "id": event_id,
        "summary": summary,
        "status": "confirmed",
        "etag": f'"{event_id}-etag"',
        "description": None,
        "location": None,
        "html_link": f"https://calendar.google.com/event?eid={event_id}",
        "start": {
            "date": None,
            "date_time": datetime.fromisoformat(start),
            "time_zone": TZ_NAME,
        },
        "end": {
            "date": None,
            "date_time": datetime.fromisoformat(end),
            "time_zone": TZ_NAME,
        },
        "attendees": [],
        "recurrence": None,
        "recurring_event_id": None,
        "original_start_time": None,
        "updated": None,
    }


def _install_calendar_service(monkeypatch, events, *, lookup_event=None):
    by_id = {event["id"]: event for event in events}
    service = SimpleNamespace(
        list_events=AsyncMock(
            return_value={
                "events": events,
                "next_page_token": None,
                "calendar_id": "primary",
            }
        ),
        get_event=AsyncMock(
            side_effect=lambda **kwargs: by_id.get(kwargs.get("event_id"))
        ),
        validate_event_write_access=AsyncMock(
            side_effect=lambda **kwargs: (
                lookup_event
                if lookup_event is not None
                else by_id.get(kwargs.get("event_id"))
            )
        ),
        list_calendars=AsyncMock(
            return_value={"calendars": [], "next_page_token": None}
        ),
        delete_event=AsyncMock(return_value={"deleted": True}),
    )
    monkeypatch.setattr(
        calendar_agent_module, "CalendarService", lambda _db: service
    )
    monkeypatch.setattr(
        calendar_agent_module, "SessionLocal", lambda: nullcontext(object())
    )
    return service


def _run_node(monkeypatch, message, events, *, state_overrides=None):
    service = _install_calendar_service(monkeypatch, events)
    state = _state(message, **(state_overrides or {}))
    route = gmail_agent_nodes.gmail_intent_router_node(dict(state))
    result = calendar_agent_module.calendar_agent_node(
        {**state, **route},
        _runtime(),
    )
    return service, route, result


def _pending_selection(candidates, **overrides):
    pending = {
        "version": 1,
        "operation": "delete_selection",
        "user_id": str(USER_ID),
        "conversation_id": str(CONVERSATION_ID),
        "connection_id": str(CONNECTION_ID),
        "calendar_id": "primary",
        "candidates": candidates,
        "created_at": "2026-11-14T14:55:00+05:30",
        "expires_at": "2026-11-14T15:05:00+05:30",
        "original_request": "delete the event from 15th november gym time",
    }
    pending.update(overrides)
    return pending


def _candidate(event):
    start = event["start"]["date_time"]
    end = event["end"]["date_time"]
    return {
        "event_id": event["id"],
        "calendar_id": "primary",
        "summary": event["summary"],
        "start": start.isoformat(),
        "end": end.isoformat(),
        "time_zone": TZ_NAME,
    }


@pytest.mark.parametrize("message", STANDALONE_DELETE_PHRASES)
def test_all_reported_delete_phrases_select_real_event_without_clarification(
    monkeypatch,
    message,
):
    target = _event("evt-real-gym-time-for")
    service, route, result = _run_node(monkeypatch, message, [target])

    assert route.get("assistant_route") == "google_calendar"
    proposal = result.get("calendar_action_proposal") or {}
    params = proposal.get("parameters") or {}
    assert params.get("event_id") == target["id"]
    assert params.get("calendar_id") == "primary"
    assert "Which Calendar event" not in result.get("assistant_response", "")
    assert result.get("pending_calendar_delete_selection") is None
    assert service.delete_event.await_count == 0

    calls = service.list_events.await_args_list
    assert calls
    for call in calls:
        query = call.kwargs.get("query")
        if query is not None:
            assert "gym" in " ".join(str(query).casefold().split())


def test_exact_phrase_date_bounds_use_local_calendar_day(monkeypatch):
    target = _event("evt-real-gym-time-for")
    service, _route, _result = _run_node(
        monkeypatch,
        "delete the event from 15th november gym time",
        [target],
    )

    call = service.list_events.await_args_list[0]
    assert call.kwargs["time_min"] == datetime(
        2026, 11, 15, 0, 0, tzinfo=TZ
    )
    assert call.kwargs["time_max"] == datetime(
        2026, 11, 16, 0, 0, tzinfo=TZ
    )
    assert call.kwargs["calendar_id"] == "primary"


def test_multiple_genuine_matches_keep_trusted_candidates_without_proposal(
    monkeypatch,
):
    events = [
        _event("evt-real-gym-time-for"),
        _event(
            "evt-real-gym-other",
            start="2026-11-15T18:00:00+05:30",
            end="2026-11-15T19:00:00+05:30",
        ),
    ]
    service, _route, result = _run_node(
        monkeypatch,
        "Delete the gym time event from November 15, 2026.",
        events,
    )

    assert result.get("calendar_action_proposal") is None
    assert "multiple possible Calendar events" in result["assistant_response"]
    pending = result["pending_calendar_delete_selection"]
    assert [c["event_id"] for c in pending["candidates"]] == [
        "evt-real-gym-time-for",
        "evt-real-gym-other",
    ]
    assert all(
        c["calendar_id"] == "primary"
        and c["start"]
        and c["end"]
        and c["time_zone"] == TZ_NAME
        for c in pending["candidates"]
    )
    assert service.delete_event.await_count == 0


def test_date_free_title_followup_stays_ambiguous_and_retains_candidates(
    monkeypatch,
):
    candidates = [
        _candidate(_event("evt-real-gym-time-for")),
        _candidate(
            _event(
                "evt-real-gym-other",
                start="2026-11-15T18:00:00+05:30",
                end="2026-11-15T19:00:00+05:30",
            )
        ),
    ]
    pending = _pending_selection(candidates)
    state = _state(
        "gym time for",
        pending_calendar_delete_selection=pending,
        pending_calendar_action={
            "version": 1,
            "operation": "delete",
            "connection_id": str(CONNECTION_ID),
        },
    )
    route = gmail_agent_nodes.gmail_intent_router_node(state)

    assert route.get("assistant_route") == "google_calendar"
    assert (route.get("gmail_plan") or {}).get("calendar_tool") == "clarify"
    assert "more than one matching Calendar event" in route["assistant_response"]
    assert route.get("calendar_action_request") is None
    retained = route["pending_calendar_delete_selection"]
    assert [c["event_id"] for c in retained["candidates"]] == [
        "evt-real-gym-time-for",
        "evt-real-gym-other",
    ]

    result = calendar_agent_module.calendar_agent_node(
        {**state, **route},
        _runtime(),
    )
    assert result.get("calendar_action_proposal") is None
    assert result.get("pending_calendar_delete_selection") is not None


def test_date_time_followup_resolves_against_trusted_candidates_only(
    monkeypatch,
):
    target = _event("evt-real-gym-time-for")
    other = _event(
        "evt-real-gym-other",
        start="2026-11-15T18:00:00+05:30",
        end="2026-11-15T19:00:00+05:30",
    )
    pending = _pending_selection([_candidate(target), _candidate(other)])
    state = _state(
        "gym time for from Nov 15, 2026, 9:00 AM – Nov 15, 2026, 10:00 AM",
        pending_calendar_delete_selection=pending,
    )
    route = gmail_agent_nodes.gmail_intent_router_node(state)

    assert (route.get("gmail_plan") or {}).get("calendar_tool") == (
        "delete_calendar_event_proposal"
    )
    assert target["id"] in route["calendar_action_request"]
    assert other["id"] not in route["calendar_action_request"]
    assert route.get("pending_calendar_delete_selection") is None

    service = _install_calendar_service(monkeypatch, [target, other])
    result = calendar_agent_module.calendar_agent_node(
        {**state, **route},
        _runtime(),
    )
    params = (result.get("calendar_action_proposal") or {}).get("parameters") or {}
    assert params.get("event_id") == target["id"]
    assert params.get("expected_etag") == '"evt-real-gym-time-for-etag"'
    assert service.list_events.await_count == 0
    assert service.delete_event.await_count == 0


def test_stale_candidate_id_never_creates_a_proposal(monkeypatch):
    stale_candidate = {
        "event_id": "evt-not-in-google-calendar",
        "calendar_id": "primary",
        "summary": "Gym time for",
        "start": "2026-11-15T09:00:00+05:30",
        "end": "2026-11-15T10:00:00+05:30",
        "time_zone": TZ_NAME,
    }
    pending = _pending_selection([stale_candidate])
    state = _state(
        "gym time for from Nov 15, 2026, 9:00 AM – Nov 15, 2026, 10:00 AM",
        pending_calendar_delete_selection=pending,
    )
    route = gmail_agent_nodes.gmail_intent_router_node(state)

    service = _install_calendar_service(
        monkeypatch,
        [_event("evt-real-gym-time-for")],
        lookup_event=None,
    )
    result = calendar_agent_module.calendar_agent_node(
        {**state, **route},
        _runtime(),
    )

    assert result.get("calendar_action_proposal") is None
    assert service.delete_event.await_count == 0


def test_pending_selection_restoration_requires_matching_scope():
    pending = _pending_selection([_candidate(_event("evt-real-gym-time-for"))])
    history = [{"role": "assistant", "metadata": {
        "pending_calendar_delete_selection": pending,
    }}]
    now = datetime(2026, 11, 14, 10, 0, tzinfo=TZ)

    assert ChatService._pending_calendar_delete_selection(
        history,
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        connection_id=str(CONNECTION_ID),
        now=now,
    ) == pending
    assert ChatService._pending_calendar_delete_selection(
        history,
        user_id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        conversation_id=CONVERSATION_ID,
        connection_id=str(CONNECTION_ID),
        now=now,
    ) is None
    assert ChatService._pending_calendar_delete_selection(
        history,
        user_id=USER_ID,
        conversation_id=uuid4(),
        connection_id=str(CONNECTION_ID),
        now=now,
    ) is None
    assert ChatService._pending_calendar_delete_selection(
        history,
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        connection_id=str(OTHER_CONNECTION_ID),
        now=now,
    ) is None


@pytest.mark.parametrize(
    "metadata",
    (
        {},
        {"pending_calendar_delete_selection": None},
        {"pending_calendar_delete_selection": {"version": 99}},
    ),
)
def test_missing_pending_selection_is_not_restored(metadata):
    assert ChatService._pending_calendar_delete_selection(
        [{"role": "assistant", "metadata": metadata}],
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        connection_id=str(CONNECTION_ID),
        now=datetime(2026, 11, 14, 10, 0, tzinfo=TZ),
    ) is None


def test_expired_pending_selection_is_not_restored():
    pending = _pending_selection(
        [_candidate(_event("evt-real-gym-time-for"))],
        expires_at="2026-11-14T09:00:00+05:30",
    )
    assert ChatService._pending_calendar_delete_selection(
        [{"role": "assistant", "metadata": {
            "pending_calendar_delete_selection": pending,
        }}],
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        connection_id=str(CONNECTION_ID),
        now=datetime(2026, 11, 14, 10, 0, tzinfo=TZ),
    ) is None


@pytest.mark.parametrize(
    ("message", "expected_query"),
    (
        ("Delete the gym time event from November 15, 2026.", "gym time"),
        ("delete the event from 15th november gym time", "gym time"),
        ("delete gym time for from Nov 15", "gym time"),
        ("gym time for", None),
        (DATE_TIME_FOLLOWUP, None),
        ("Delete Gym time for on November 15", "Gym time"),
        ("Remove my gym event on November 15 at 9 AM", "gym"),
    ),
)
def test_parser_normalizes_every_reported_phrase(message, expected_query):
    details = parse_calendar_delete_request(
        message,
        planner_time_zone=TZ_NAME,
        reference_time=datetime(2026, 11, 14, 15, 0, tzinfo=TZ),
    )
    assert details.get("event_query") == expected_query
    if message != "gym time for":
        assert details.get("event_date") == datetime(2026, 11, 15).date()


def test_parser_extracts_exact_followup_temporal_constraints():
    details = parse_calendar_delete_request(
        "gym time for from Nov 15, 2026, 9:00 AM – Nov 15, 2026, 10:00 AM",
        planner_time_zone=TZ_NAME,
        reference_time=datetime(2026, 11, 14, 15, 0, tzinfo=TZ),
    )
    assert details["event_date"] == datetime(2026, 11, 15).date()
    assert details["original_start_time"].strftime("%H:%M") == "09:00"
    assert details["original_end_time"].strftime("%H:%M") == "10:00"


class _FakeRequestRepository:
    def create_request(self, **_kwargs):
        request_id = uuid4()
        return {"id": request_id, "status": "processing"}, True

    def claim_request(self, **_kwargs):
        return {"id": uuid4()}

    def attach_user_message(self, **_kwargs):
        return {"attached": True}

    def mark_execution_started(self, **_kwargs):
        return {"started": True}

    def update_status(self, **_kwargs):
        return {"updated": True}


class _FakeMessageService:
    def __init__(self):
        self.messages = []

    def verify_conversation_ownership(self, **_kwargs):
        return True

    def get_request_message(self, *, role, chat_request_id=None, **_kwargs):
        return next(
            (
                message for message in reversed(self.messages)
                if message["role"] == role
                and (
                    chat_request_id is None
                    or message.get("chat_request_id") == chat_request_id
                )
            ),
            None,
        )

    def create_request_message(
        self,
        *,
        conversation_id,
        role,
        content,
        metadata=None,
        chat_request_id=None,
        **_kwargs,
    ):
        message = {
            "id": uuid4(),
            "conversation_id": conversation_id,
            "chat_request_id": chat_request_id,
            "role": role,
            "content": content,
            "metadata": metadata or {},
            "created_at": datetime.now(timezone.utc),
        }
        self.messages.append(message)
        return message

    def get_message(self, *, message_id, **_kwargs):
        return next(
            (m for m in self.messages if m["id"] == message_id),
            None,
        )

    def get_conversation_messages(self, **_kwargs):
        return [dict(message) for message in self.messages]


class _FakeUserConnectionService:
    db = object()

    def get_user_calendar_access(self, **_kwargs):
        return {
            "google_connection_available": True,
            "capabilities": {
                "write_permission_granted": True,
                "calendar_write_execution_available": False,
                "calendar_update_execution_available": False,
                "calendar_create_execution_available": False,
                "calendar_delete_execution_available": True,
                "calendar_update_feature_enabled": False,
            },
            "event_read_connection_id": str(CONNECTION_ID),
            "freebusy_connection_id": None,
            "write_connection_id": str(CONNECTION_ID),
            "default_connection_invalid": False,
            "default_connection_selected": True,
            "selected_connection_email": "user@example.com",
            "event_selection_required": False,
            "freebusy_selection_required": False,
            "selected_event_scope_missing": False,
            "selected_freebusy_scope_missing": False,
            "calendar_write_selection_required": False,
            "calendar_selected_write_scope_missing": False,
        }

    def get_user_contacts_access(self, **_kwargs):
        return {
            "google_connection_available": False,
            "capabilities": {},
            "read_connection_id": None,
            "write_connection_id": None,
            "selected_connection_email": None,
            "default_connection_invalid": False,
            "default_connection_selected": False,
            "selection_required": False,
            "candidate_emails": [],
            "read_scope_missing": False,
            "write_scope_missing": False,
        }

    def get_user_drive_access(self, **_kwargs):
        return {
            "google_connection_available": False,
            "capabilities": {},
            "connection_id": None,
            "selected_connection_email": None,
            "default_connection_invalid": False,
            "default_connection_selected": False,
            "selection_required": False,
            "candidate_emails": [],
            "read_scope_missing": False,
            "enabled": False,
        }


def _install_fake_approval_service(monkeypatch, proposal_calls):
    class _FakeActionApprovalService:
        def __init__(self, _db):
            pass

        def create_proposal(self, **kwargs):
            payload = kwargs["payload"]
            params = getattr(payload, "parameters", None)
            action_type = getattr(payload, "action_type", None)
            proposal_calls.append(
                {
                    "action_type": action_type,
                    "parameters": params,
                }
            )
            return SimpleNamespace(
                model_dump=lambda mode="json": {
                    "proposal_id": str(uuid4()),
                    "action_type": action_type,
                    "parameters": params,
                    "status": "pending",
                }
            )

    monkeypatch.setattr(
        chat_service_module,
        "ActionApprovalService",
        _FakeActionApprovalService,
    )


def _new_chat_service(message_service):
    service = ChatService(
        message_service=message_service,
        user_settings_service=SimpleNamespace(
            get_settings=Mock(
                return_value=SimpleNamespace(memory_enabled=False)
            )
        ),
        memory_pipeline=SimpleNamespace(process=Mock(return_value=None)),
        request_repository=_FakeRequestRepository(),
        user_connection_service=_FakeUserConnectionService(),
    )
    return service


def test_restored_three_message_sequence_resolves_original_phase27_report(
    monkeypatch,
):
    target = _event("evt-real-gym-time-for")
    other = _event(
        "evt-real-gym-other",
        start="2026-11-15T18:00:00+05:30",
        end="2026-11-15T19:00:00+05:30",
    )
    calendar_service = _install_calendar_service(
        monkeypatch,
        [target, other],
    )
    proposal_calls = []
    _install_fake_approval_service(monkeypatch, proposal_calls)
    monkeypatch.setattr(
        ChatService,
        "_start_heartbeat",
        lambda *_args, **_kwargs: (None, None, None),
    )
    monkeypatch.setattr(
        ChatService,
        "_stop_heartbeat",
        lambda *_args, **_kwargs: None,
    )
    message_service = _FakeMessageService()

    def _send(content):
        # Assistant UI restoration creates a fresh service from persisted history.
        service = _new_chat_service(message_service)
        token = current_request_metrics.set(
            RequestMetrics(request_id=str(uuid4()))
        )
        try:
            return service.send_message(
                user_id=USER_ID,
                conversation_id=CONVERSATION_ID,
                content=content,
                idempotency_key=uuid4(),
                user_timezone=TZ_NAME,
                default_calendar_connection_id=str(CONNECTION_ID),
                calendar_enabled=True,
                gmail_enabled=False,
                contacts_enabled=False,
                drive_enabled=False,
            )
        finally:
            current_request_metrics.reset(token)

    first = _send("delete the event from 15th november gym time")
    first_meta = first["assistant_message"]["metadata"]
    assert "multiple possible Calendar events" in first["assistant_message"]["content"]
    assert [
        c["event_id"]
        for c in first_meta["pending_calendar_delete_selection"]["candidates"]
    ] == ["evt-real-gym-time-for", "evt-real-gym-other"]

    second = _send("gym time for")
    second_meta = second["assistant_message"]["metadata"]
    assert "more than one matching Calendar event" in second["assistant_message"]["content"]
    assert [
        c["event_id"]
        for c in second_meta["pending_calendar_delete_selection"]["candidates"]
    ] == ["evt-real-gym-time-for", "evt-real-gym-other"]
    assert proposal_calls == []

    third = _send(
        "gym time for from Nov 15, 2026, 9:00 AM – Nov 15, 2026, 10:00 AM"
    )
    third_meta = third["assistant_message"]["metadata"]
    assert "prepared an approval request" in third["assistant_message"]["content"]
    assert third_meta["pending_calendar_delete_selection"] is None
    assert len(proposal_calls) == 1
    assert proposal_calls[0]["action_type"] == "calendar.delete_event"
    params = proposal_calls[0]["parameters"]
    assert isinstance(params, dict)
    assert params["event_id"] == "evt-real-gym-time-for"
    assert params["calendar_id"] == "primary"
    assert params["expected_etag"] == '"evt-real-gym-time-for-etag"'

    # Only the first turn needed a Calendar query; follow-ups used trusted IDs.
    assert calendar_service.list_events.await_count == 1
    assert calendar_service.delete_event.await_count == 0
