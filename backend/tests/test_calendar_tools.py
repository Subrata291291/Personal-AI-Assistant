import asyncio
import json
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.workflows import calendar_tools
from app.workflows.calendar_tools import (
    CheckCalendarAvailabilityInput,
    GetCalendarScheduleInput,
    ListCalendarEventsInput,
    create_calendar_tools,
)
from app.services.calendar_service import CalendarService


USER_ID = uuid4()
OTHER_USER_ID = uuid4()
CONNECTION_ID = uuid4()
OTHER_CONNECTION_ID = uuid4()


class FakeDatabase:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def build_tools(
    monkeypatch,
    service,
    *,
    user_id=USER_ID,
    connection_id=CONNECTION_ID,
    request_time=None,
):
    database = FakeDatabase()
    db_factory = Mock(return_value=database)
    monkeypatch.setattr(
        calendar_tools,
        "CalendarService",
        lambda _db: service,
    )
    tools = create_calendar_tools(
        user_id=user_id,
        connection_id=connection_id,
        request_time=request_time,
        db_factory=db_factory,
    )
    return {tool.name: tool for tool in tools}, db_factory


def invoke(tool, arguments):
    return json.loads(asyncio.run(tool.ainvoke(arguments)))


def test_factory_registers_only_read_tools_and_hides_identity_arguments():
    tools = create_calendar_tools(
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        db_factory=FakeDatabase,
    )

    assert {tool.name for tool in tools} == {
        "list_calendars",
        "list_calendar_events",
        "get_calendar_event",
        "get_today_schedule",
        "get_calendar_schedule",
        "check_calendar_availability",
    }
    for tool in tools:
        properties = tool.args_schema.model_json_schema()["properties"]
        assert "user_id" not in properties
        assert "connection_id" not in properties
        assert "create_event" not in tool.name
        assert "delete_event" not in tool.name
        assert "update_event" not in tool.name


def test_factory_registers_operations_independently_for_granted_scopes():
    event_tools = create_calendar_tools(
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        include_event_tools=True,
        include_freebusy_tool=False,
        db_factory=FakeDatabase,
    )
    freebusy_tools = create_calendar_tools(
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        include_event_tools=False,
        include_freebusy_tool=True,
        db_factory=FakeDatabase,
    )

    assert {tool.name for tool in event_tools} == {
        "list_calendars",
        "list_calendar_events",
        "get_calendar_event",
        "get_today_schedule",
        "get_calendar_schedule",
    }
    assert [tool.name for tool in freebusy_tools] == [
        "check_calendar_availability",
    ]


def test_list_calendars_tool_uses_selected_user_and_connection(monkeypatch):
    service = SimpleNamespace(list_calendars=AsyncMock(return_value={
        "calendars": [{
            "id": "work-calendar-id",
            "summary": "Work",
            "primary": False,
            "access_role": "writer",
            "time_zone": "Asia/Kolkata",
        }],
    }))
    tools, db_factory = build_tools(monkeypatch, service)

    output = invoke(tools["list_calendars"], {})

    assert output == {
        "ok": True,
        "data": {
            "calendars": [{
                "id": "work-calendar-id",
                "summary": "Work",
                "primary": False,
                "access_role": "writer",
                "time_zone": "Asia/Kolkata",
            }],
        },
    }
    service.list_calendars.assert_awaited_once_with(
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        max_results=100,
        page_token=None,
    )
    assert db_factory.call_count == 1


def test_list_calendars_missing_scope_returns_targeted_reauthorization_guidance(
    monkeypatch,
):
    service = SimpleNamespace(
        list_calendars=AsyncMock(side_effect=PermissionError("scope missing")),
    )
    tools, _ = build_tools(monkeypatch, service)

    output = invoke(tools["list_calendars"], {})

    assert output["ok"] is False
    assert output["error"]["code"] == "calendar_list_permission_missing"
    assert "Grant Calendar List Access" in output["error"]["message"]


def test_list_events_tool_returns_json_safe_structured_result(monkeypatch):
    event = {
        "id": "event-1",
        "start": {"date_time": datetime(2026, 11, 5, tzinfo=timezone.utc)},
    }
    service = SimpleNamespace(list_events=AsyncMock(return_value={
        "calendar_id": "primary",
        "events": [event],
        "next_page_token": "next-page",
        "time_zone": "UTC",
    }))
    tools, db_factory = build_tools(monkeypatch, service)

    output = invoke(tools["list_calendar_events"], {
        "time_min": "2026-11-05T00:00:00Z",
        "time_max": "2026-11-06T00:00:00Z",
        "query": "planning",
        "attendee": "person@example.com",
        "time_zone": "UTC",
        "max_results": 25,
        "page_token": "page-token",
    })

    assert output == {
        "ok": True,
        "data": {
            "calendar_id": "primary",
            "events": [{
                "id": "event-1",
                "start": {
                "date_time": "2026-11-05T00:00:00+00:00",
                },
            }],
            "next_page_token": "next-page",
            "time_zone": "UTC",
        },
    }
    service.list_events.assert_awaited_once()
    kwargs = service.list_events.await_args.kwargs
    assert kwargs["user_id"] == USER_ID
    assert kwargs["connection_id"] == CONNECTION_ID
    assert kwargs["calendar_id"] == "primary"
    assert kwargs["max_results"] == 25
    assert kwargs["page_token"] == "page-token"
    assert kwargs["attendee"] == "person@example.com"
    assert db_factory.call_count == 1


def test_today_tool_uses_the_captured_request_time(monkeypatch):
    request_time = datetime.fromisoformat("2026-11-06T18:45:00+00:00")
    service = SimpleNamespace(
        get_today_schedule=AsyncMock(return_value={
            "calendar_id": "primary",
            "date": date(2026, 11, 7),
            "period": "day",
            "time_zone": "Asia/Kolkata",
            "events": [],
            "conflicts": [],
            "conflicts_complete": True,
            "next_page_token": None,
        })
    )
    tools, _ = build_tools(
        monkeypatch,
        service,
        request_time=request_time.isoformat(),
    )

    output = invoke(tools["get_today_schedule"], {
        "time_zone": "Asia/Kolkata",
    })

    assert output["ok"] is True
    assert service.get_today_schedule.await_args.kwargs["request_time"] == (
        request_time
    )


@pytest.mark.parametrize(
    ("name", "arguments", "method", "expected"),
    [
        (
            "get_calendar_event",
            {"event_id": "event-2", "calendar_id": "shared@example.com"},
            "get_event",
            {
                "event_id": "event-2",
                "calendar_id": "shared@example.com",
            },
        ),
        (
            "get_today_schedule",
            {"time_zone": "America/Los_Angeles"},
            "get_today_schedule",
            {
                "time_zone": "America/Los_Angeles",
                "calendar_id": "primary",
            },
        ),
        (
            "get_calendar_schedule",
            {
                "date": "2026-11-05",
                "period": "week",
                "time_zone": "Europe/London",
            },
            "get_schedule",
            {
                "start_date": date(2026, 11, 5),
                "period": "week",
                "time_zone": "Europe/London",
                "calendar_id": "primary",
            },
        ),
        (
            "get_calendar_schedule",
            {
                "date": "2026-11-05",
                "period": "month",
                "time_zone": "Asia/Kolkata",
            },
            "get_schedule",
            {
                "start_date": date(2026, 11, 5),
                "period": "month",
                "time_zone": "Asia/Kolkata",
                "calendar_id": "primary",
            },
        ),
        (
            "check_calendar_availability",
            {
                "time_min": "2026-11-05T09:00:00-08:00",
                "time_max": "2026-11-05T10:00:00-08:00",
                "time_zone": "America/Los_Angeles",
                "calendar_ids": ["primary", "shared@example.com"],
            },
            "query_freebusy",
            {
                "time_min": datetime.fromisoformat(
                    "2026-11-05T09:00:00-08:00"
                ),
                "time_max": datetime.fromisoformat(
                    "2026-11-05T10:00:00-08:00"
                ),
                "time_zone": "America/Los_Angeles",
                "calendar_ids": ["primary", "shared@example.com"],
            },
        ),
    ],
)
def test_each_calendar_tool_calls_its_existing_service_method(
    monkeypatch,
    name,
    arguments,
    method,
    expected,
):
    service_method = AsyncMock(return_value={"result": "ok"})
    service = SimpleNamespace(**{method: service_method})
    tools, _ = build_tools(monkeypatch, service)

    assert invoke(tools[name], arguments) == {
        "ok": True,
        "data": {"result": "ok"},
    }
    kwargs = service_method.await_args.kwargs
    assert kwargs.pop("user_id") == USER_ID
    assert kwargs.pop("connection_id") == CONNECTION_ID
    assert kwargs == expected


def test_invalid_tool_arguments_are_rejected_before_service_execution(
    monkeypatch,
):
    service = SimpleNamespace(list_events=AsyncMock(return_value={}))
    tools, _ = build_tools(monkeypatch, service)

    with pytest.raises(ValidationError):
        ListCalendarEventsInput.model_validate({
            "time_min": "2026-11-05T10:00:00",
            "user_id": str(OTHER_USER_ID),
        })
    with pytest.raises(ValidationError):
        CheckCalendarAvailabilityInput.model_validate({
            "time_min": "2026-11-05T10:00:00Z",
            "time_max": "2026-11-05T09:00:00Z",
            "time_zone": "UTC",
        })
    with pytest.raises(ValidationError):
        GetCalendarScheduleInput.model_validate({
            "date": "2026-11-05",
            "period": "year",
            "time_zone": "UTC",
        })
    with pytest.raises(ValidationError):
        asyncio.run(tools["list_calendar_events"].ainvoke({
            "user_id": str(OTHER_USER_ID),
            "connection_id": str(OTHER_CONNECTION_ID),
        }))

    service.list_events.assert_not_awaited()


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (
            ValueError("Connected Google account was not found."),
            "calendar_connection_missing",
        ),
        (
            PermissionError("Google Calendar permission has not been granted."),
            "calendar_permission_missing",
        ),
        (
            ValueError("Google OAuth credentials were not found."),
            "calendar_credentials_missing",
        ),
    ],
)
def test_missing_connection_credentials_or_scope_return_safe_errors(
    monkeypatch,
    error,
    expected_code,
):
    async def fail(**_kwargs):
        raise error

    service = SimpleNamespace(list_events=fail)
    tools, _ = build_tools(monkeypatch, service)

    output = invoke(tools["list_calendar_events"], {})

    assert output["ok"] is False
    assert output["error"]["code"] == expected_code
    assert output["error"]["message"]
    assert str(USER_ID) not in json.dumps(output)
    assert str(CONNECTION_ID) not in json.dumps(output)


@pytest.mark.parametrize(
    ("status_code", "retryable", "expected_code"),
    [
        (401, False, "calendar_authorization_expired"),
        (403, False, "calendar_permission_denied"),
        (404, False, "calendar_resource_not_found"),
        (503, True, "calendar_temporarily_unavailable"),
    ],
)
def test_provider_errors_are_sanitized(
    monkeypatch,
    status_code,
    retryable,
    expected_code,
):
    async def fail(**_kwargs):
        raise calendar_tools.GoogleAPIError(
            status_code,
            "sensitive provider detail refresh_token=secret",
            retryable=retryable,
        )

    service = SimpleNamespace(get_event=fail)
    tools, _ = build_tools(monkeypatch, service)

    output = invoke(tools["get_calendar_event"], {"event_id": "event-1"})

    assert output["ok"] is False
    assert output["error"]["code"] == expected_code
    assert "secret" not in json.dumps(output)
    assert "sensitive" not in json.dumps(output)


def test_tools_bind_user_and_connection_separately_and_graph_routes_calendar(
    monkeypatch,
):
    invocations = []

    async def list_events(**kwargs):
        invocations.append(kwargs)
        return {"events": []}

    service = SimpleNamespace(list_events=list_events)
    tools_one, _ = build_tools(monkeypatch, service)
    tools_two = create_calendar_tools(
        user_id=OTHER_USER_ID,
        connection_id=OTHER_CONNECTION_ID,
        db_factory=FakeDatabase,
    )

    assert invoke(tools_one["list_calendar_events"], {}) == {
        "ok": True,
        "data": {"events": []},
    }
    # The second factory's bound identity is verified when invoked against
    # the same service implementation.
    invoke(
        next(tool for tool in tools_two if tool.name == "list_calendar_events"),
        {},
    )

    assert invocations[0]["user_id"] == USER_ID
    assert invocations[0]["connection_id"] == CONNECTION_ID
    assert invocations[1]["user_id"] == OTHER_USER_ID
    assert invocations[1]["connection_id"] == OTHER_CONNECTION_ID

    from app.workflows.chat_graph import ASSISTANT_ROUTE_NODES

    assert ASSISTANT_ROUTE_NODES["google_calendar"] == "calendar_agent"
    assert ASSISTANT_ROUTE_NODES["gmail"] == "gmail_agent"


@pytest.mark.parametrize(
    ("selected_user_id", "selected_connection_id", "scopes", "tool_name", "expected_code"),
    [
        (
            OTHER_USER_ID,
            CONNECTION_ID,
            [CalendarService.CALENDAR_EVENTS_READ_SCOPE],
            "list_calendar_events",
            "calendar_connection_missing",
        ),
        (
            USER_ID,
            CONNECTION_ID,
            [],
            "list_calendar_events",
            "calendar_permission_missing",
        ),
        (
            USER_ID,
            CONNECTION_ID,
            [CalendarService.CALENDAR_EVENTS_READ_SCOPE],
            "check_calendar_availability",
            "calendar_permission_missing",
        ),
    ],
)
def test_tool_calls_enforce_owned_connection_and_per_operation_scope(
    monkeypatch,
    selected_user_id,
    selected_connection_id,
    scopes,
    tool_name,
    expected_code,
):
    service = CalendarService(db=None)
    google_calls = []
    credential_calls = []

    def get_connection(*, user_id, connection_id):
        if (user_id, connection_id) == (USER_ID, CONNECTION_ID):
            return object()
        return None

    def get_credential(*, user_id, connection_id):
        credential_calls.append((user_id, connection_id))
        return SimpleNamespace(scopes=scopes)

    service.connection_repository = SimpleNamespace(
        get_active_google_connection=get_connection,
    )
    service.credential_repository = SimpleNamespace(
        get_by_connection=get_credential,
    )

    async def google_get(**kwargs):
        google_calls.append(("GET", kwargs))
        return {"items": []}

    async def freebusy_query(**kwargs):
        google_calls.append(("POST", kwargs))
        return {"calendars": {}}

    service._google_get = google_get
    service._freebusy_query = freebusy_query
    monkeypatch.setattr(calendar_tools, "CalendarService", lambda _db: service)
    tools = {
        tool.name: tool
        for tool in create_calendar_tools(
            user_id=selected_user_id,
            connection_id=selected_connection_id,
            db_factory=FakeDatabase,
        )
    }

    arguments = (
        {
            "time_min": "2026-11-05T09:00:00Z",
            "time_max": "2026-11-05T10:00:00Z",
            "time_zone": "UTC",
        }
        if tool_name == "check_calendar_availability"
        else {}
    )
    output = invoke(tools[tool_name], arguments)

    assert output["ok"] is False
    assert output["error"]["code"] == expected_code
    assert google_calls == []
    if expected_code == "calendar_connection_missing":
        assert credential_calls == []
    else:
        assert credential_calls == [(USER_ID, CONNECTION_ID)]
