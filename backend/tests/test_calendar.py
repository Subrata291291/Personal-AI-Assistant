import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.api.v1.endpoints import calendar as calendar_endpoint
from app.db.session import get_db
from app.integrations.google.api_client import GoogleAPIClient
from app.integrations.google.exceptions import GoogleAPIError
from app.main import app
from app.services import calendar_service as calendar_service_module
from app.services.calendar_service import CalendarService


USER_ID = uuid4()
OTHER_USER_ID = uuid4()
CONNECTION_ID = uuid4()
OTHER_CONNECTION_ID = uuid4()
CALENDAR_SCOPE = CalendarService.CALENDAR_EVENTS_READ_SCOPE
FREEBUSY_SCOPE = CalendarService.CALENDAR_FREEBUSY_SCOPE
UTC = timezone.utc


def make_service(scopes=None, connection=None):
    service = CalendarService(db=None)
    calls = {}

    def get_connection(*, user_id, connection_id):
        calls["connection"] = (user_id, connection_id)
        return connection if connection is not None else object()

    credential = SimpleNamespace(
        scopes=list(
            scopes
            if scopes is not None
            else [CALENDAR_SCOPE, FREEBUSY_SCOPE]
        )
    )
    service.connection_repository = SimpleNamespace(
        get_active_google_connection=get_connection,
    )
    service.credential_repository = SimpleNamespace(
        get_by_connection=lambda **_: credential,
    )
    return service, calls


def aware(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_missing_calendar_connection_is_not_found_and_never_calls_google():
    service, calls = make_service(connection=None)

    def missing_connection(*, user_id, connection_id):
        calls["connection"] = (user_id, connection_id)
        return None

    service.connection_repository.get_active_google_connection = missing_connection
    google_calls = []

    async def fake_google_get(**kwargs):
        google_calls.append(kwargs)
        return {}

    service._google_get = fake_google_get

    with pytest.raises(ValueError, match="account was not found"):
        asyncio.run(service.list_events(USER_ID, CONNECTION_ID))

    assert calls["connection"] == (USER_ID, CONNECTION_ID)
    assert not google_calls


def test_calendar_scope_is_required_and_connection_is_user_scoped():
    service, calls = make_service(scopes=[])
    google_calls = []

    async def fake_google_get(**kwargs):
        google_calls.append(kwargs)
        return {}

    service._google_get = fake_google_get

    with pytest.raises(PermissionError, match="permission has not been granted"):
        asyncio.run(
            service.list_events(OTHER_USER_ID, OTHER_CONNECTION_ID)
        )

    assert calls["connection"] == (OTHER_USER_ID, OTHER_CONNECTION_ID)
    assert not google_calls


def test_wrong_user_or_connection_cannot_use_an_owned_account():
    service, calls = make_service()
    google_calls = []

    def only_owner_can_resolve(*, user_id, connection_id):
        calls["connection"] = (user_id, connection_id)
        if (user_id, connection_id) == (USER_ID, CONNECTION_ID):
            return object()
        return None

    service.connection_repository.get_active_google_connection = (
        only_owner_can_resolve
    )

    async def fake_google_get(**kwargs):
        google_calls.append(kwargs)
        return {"items": []}

    service._google_get = fake_google_get

    with pytest.raises(ValueError, match="account was not found"):
        asyncio.run(service.list_events(OTHER_USER_ID, CONNECTION_ID))
    with pytest.raises(ValueError, match="account was not found"):
        asyncio.run(service.list_events(USER_ID, OTHER_CONNECTION_ID))

    assert not google_calls


@pytest.mark.parametrize(
    "scope",
    [
        CALENDAR_SCOPE,
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/calendar",
        "https://www.googleapis.com/auth/calendar.events",
    ],
)
def test_actual_calendar_read_scopes_are_accepted(scope):
    service, _ = make_service(scopes=[scope])

    async def fake_google_get(**_kwargs):
        return {"items": []}

    service._google_get = fake_google_get
    result = asyncio.run(service.list_events(USER_ID, CONNECTION_ID))

    assert result["calendar_id"] == "primary"


def test_event_read_does_not_accept_freebusy_only_scope():
    service, _ = make_service(scopes=[FREEBUSY_SCOPE])
    google_calls = []

    async def fake_google_get(**kwargs):
        google_calls.append(kwargs)
        return {"items": []}

    service._google_get = fake_google_get

    with pytest.raises(PermissionError):
        asyncio.run(service.list_events(USER_ID, CONNECTION_ID))

    assert not google_calls


def test_event_list_uses_primary_calendar_range_search_and_pagination():
    service, _ = make_service()
    request = {}

    async def fake_google_get(**kwargs):
        request.update(kwargs)
        return {
            "items": [
                {
                    "id": "event-match",
                    "summary": "Planning",
                    "attendees": [{"email": "person@example.com"}],
                },
                {
                    "id": "event-other",
                    "summary": "Other",
                    "attendees": [{"email": "other@example.com"}],
                },
            ],
            "nextPageToken": "next-page",
            "timeZone": "America/Los_Angeles",
        }

    service._google_get = fake_google_get
    result = asyncio.run(
        service.list_events(
            USER_ID,
            CONNECTION_ID,
            time_min=aware("2026-11-05T09:00:00-08:00"),
            time_max=aware("2026-11-06T09:00:00-08:00"),
            query="Planning",
            attendee="PERSON@example.com",
            page_token="previous-page",
            time_zone="America/Los_Angeles",
        )
    )

    assert request["endpoint"] == "calendars/primary/events"
    assert request["params"] == {
        "maxResults": 100,
        "timeMin": "2026-11-05T17:00:00Z",
        "timeMax": "2026-11-06T17:00:00Z",
        "singleEvents": "true",
        "orderBy": "startTime",
        "pageToken": "previous-page",
        "q": "Planning",
        "timeZone": "America/Los_Angeles",
    }
    assert [event["id"] for event in result["events"]] == ["event-match"]
    assert result["next_page_token"] == "next-page"
    assert result["time_zone"] == "America/Los_Angeles"


def test_event_detail_encodes_ids_and_normalizes_recurring_event():
    service, _ = make_service()
    request = {}

    async def fake_google_get(**kwargs):
        request.update(kwargs)
        return {
            "id": "event_123",
            "summary": "Recurring meeting",
            "start": {
                "dateTime": "2026-11-05T09:30:00-08:00",
                "timeZone": "America/Los_Angeles",
            },
            "end": {
                "dateTime": "2026-11-05T10:00:00-08:00",
                "timeZone": "America/Los_Angeles",
            },
            "recurrence": ["RRULE:FREQ=WEEKLY"],
            "recurringEventId": "master-1",
            "originalStartTime": {
                "dateTime": "2026-11-05T09:30:00-08:00",
                "timeZone": "America/Los_Angeles",
            },
        }

    service._google_get = fake_google_get
    event = asyncio.run(
        service.get_event(
            USER_ID,
            CONNECTION_ID,
            "event_123",
            "person@example.com",
        )
    )

    assert request["endpoint"] == (
        "calendars/person%40example.com/events/event_123"
    )
    assert event["start"]["date_time"] == aware("2026-11-05T17:30:00Z")
    assert event["start"]["time_zone"] == "America/Los_Angeles"
    assert event["recurrence"] == ["RRULE:FREQ=WEEKLY"]
    assert event["recurring_event_id"] == "master-1"
    assert event["original_start_time"]["date_time"] == aware(
        "2026-11-05T17:30:00Z"
    )


@pytest.mark.parametrize(
    "event_id,calendar_id",
    [
        ("../events/other", "primary"),
        ("..", "primary"),
        ("event-1", "primary/../other"),
        ("", "primary"),
    ],
)
def test_invalid_calendar_and_event_ids_are_rejected(event_id, calendar_id):
    service, _ = make_service()
    google_calls = []

    async def fake_google_get(**kwargs):
        google_calls.append(kwargs)
        return {}

    service._google_get = fake_google_get

    with pytest.raises(ValueError):
        asyncio.run(
            service.get_event(
                USER_ID,
                CONNECTION_ID,
                event_id,
                calendar_id,
            )
        )
    assert not google_calls


def test_naive_provider_datetime_without_timezone_is_rejected():
    with pytest.raises(GoogleAPIError, match="invalid event timezone"):
        CalendarService._normalize_event({
            "id": "event-1",
            "start": {"dateTime": "2026-11-05T09:00:00"},
        })


def test_provider_local_datetime_uses_named_iana_timezone():
    event = CalendarService._normalize_event({
        "id": "event-1",
        "start": {
            "dateTime": "2026-11-05T09:00:00",
            "timeZone": "America/Los_Angeles",
        },
        "end": {
            "dateTime": "2026-11-05T10:00:00",
            "timeZone": "America/Los_Angeles",
        },
    })

    assert event["start"]["date_time"] == aware("2026-11-05T17:00:00Z")


@pytest.mark.parametrize(
    "local_time",
    [
        "2026-11-01T01:30:00",
        "2026-03-08T02:30:00",
    ],
)
def test_ambiguous_or_nonexistent_provider_local_time_is_rejected(local_time):
    with pytest.raises(GoogleAPIError, match="invalid event timezone"):
        CalendarService._normalize_event({
            "id": "event-1",
            "start": {
                "dateTime": local_time,
                "timeZone": "America/Los_Angeles",
            },
        })


def test_all_day_event_retains_exclusive_date_boundaries():
    event = CalendarService._normalize_event({
        "id": "all-day",
        "start": {"date": "2026-11-05"},
        "end": {"date": "2026-11-06"},
    })

    assert event["start"]["date"] == date(2026, 11, 5)
    assert event["start"]["date_time"] is None
    assert event["end"]["date"] == date(2026, 11, 6)


def test_schedule_uses_local_day_boundaries_across_dst():
    service, _ = make_service()
    request = {}

    async def fake_list_events(**kwargs):
        request.update(kwargs)
        return {
            "calendar_id": "primary",
            "events": [],
            "next_page_token": None,
            "time_zone": kwargs["time_zone"],
        }

    service.list_events = fake_list_events
    result = asyncio.run(
        service.get_schedule(
            USER_ID,
            CONNECTION_ID,
            date(2026, 3, 8),
            "day",
            "America/Los_Angeles",
        )
    )

    assert request["time_min"] == aware("2026-03-08T08:00:00Z")
    assert request["time_max"] == aware("2026-03-09T07:00:00Z")
    assert result["date"] == date(2026, 3, 8)
    assert result["time_zone"] == "America/Los_Angeles"


def test_week_schedule_starts_on_monday():
    service, _ = make_service()
    request = {}

    async def fake_list_events(**kwargs):
        request.update(kwargs)
        return {
            "calendar_id": "primary",
            "events": [],
            "next_page_token": None,
            "time_zone": kwargs["time_zone"],
        }

    service.list_events = fake_list_events
    result = asyncio.run(
        service.get_schedule(
            USER_ID,
            CONNECTION_ID,
            date(2026, 11, 5),
            "week",
            "UTC",
        )
    )

    assert request["time_min"] == aware("2026-11-02T00:00:00Z")
    assert request["time_max"] == aware("2026-11-09T00:00:00Z")
    assert result["date"] == date(2026, 11, 2)


def test_invalid_or_naive_ranges_and_timezones_are_rejected():
    service, _ = make_service()

    async def fake_google_get(**_kwargs):
        return {"items": []}

    service._google_get = fake_google_get
    with pytest.raises(ValueError, match="UTC offset"):
        asyncio.run(
            service.list_events(
                USER_ID,
                CONNECTION_ID,
                time_min=datetime(2026, 11, 5, 9),
            )
        )
    with pytest.raises(ValueError, match="IANA"):
        asyncio.run(
            service.get_schedule(
                USER_ID,
                CONNECTION_ID,
                date(2026, 11, 5),
                "day",
                "Not/A_Timezone",
            )
        )


def test_freebusy_returns_busy_ranges_and_partial_calendar_errors():
    service, _ = make_service()
    request = {}

    async def fake_freebusy_query(**kwargs):
        request.update(kwargs)
        return {
            "calendars": {
                "primary": {
                    "busy": [{
                        "start": "2026-11-05T17:00:00Z",
                        "end": "2026-11-05T18:00:00Z",
                    }],
                },
                "shared@example.com": {
                    "busy": [],
                    "errors": [{
                        "domain": "calendar",
                        "reason": "notFound",
                    }],
                },
            },
        }

    service._freebusy_query = fake_freebusy_query
    result = asyncio.run(
        service.query_freebusy(
            USER_ID,
            CONNECTION_ID,
            ["primary", "shared@example.com"],
            aware("2026-11-05T09:00:00-08:00"),
            aware("2026-11-05T17:00:00-08:00"),
            "America/Los_Angeles",
        )
    )

    assert request["body"]["items"] == [
        {"id": "primary"},
        {"id": "shared@example.com"},
    ]
    assert request["body"]["timeZone"] == "America/Los_Angeles"
    assert result["calendars"]["primary"]["busy"][0]["start"] == aware(
        "2026-11-05T17:00:00Z"
    )
    assert result["calendars"]["shared@example.com"]["errors"] == [
        {"domain": "calendar", "reason": "notFound"}
    ]


def test_freebusy_requires_valid_range_timezone_and_freebusy_scope():
    service, _ = make_service(scopes=[])

    with pytest.raises(PermissionError):
        asyncio.run(
            service.query_freebusy(
                USER_ID,
                CONNECTION_ID,
                ["primary"],
                aware("2026-11-05T00:00:00Z"),
                aware("2026-11-06T00:00:00Z"),
                "UTC",
            )
        )

    service, _ = make_service()
    with pytest.raises(ValueError, match="later"):
        asyncio.run(
            service.query_freebusy(
                USER_ID,
                CONNECTION_ID,
                ["primary"],
                aware("2026-11-06T00:00:00Z"),
                aware("2026-11-05T00:00:00Z"),
                "UTC",
            )
        )


@pytest.mark.parametrize(
    "scope",
    [
        FREEBUSY_SCOPE,
        "https://www.googleapis.com/auth/calendar.freebusy",
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/calendar",
    ],
)
def test_freebusy_accepts_only_documented_freebusy_scopes(scope):
    service, _ = make_service(scopes=[scope])

    async def fake_freebusy_query(**_kwargs):
        return {"calendars": {"primary": {"busy": []}}}

    service._freebusy_query = fake_freebusy_query
    result = asyncio.run(
        service.query_freebusy(
            USER_ID,
            CONNECTION_ID,
            ["primary"],
            aware("2026-11-05T00:00:00Z"),
            aware("2026-11-06T00:00:00Z"),
            "UTC",
        )
    )

    assert "primary" in result["calendars"]


@pytest.mark.parametrize(
    "scope",
    [
        "https://www.googleapis.com/auth/calendar.events",
        "https://www.googleapis.com/auth/calendar.events.readonly",
    ],
)
def test_event_scopes_alone_do_not_authorize_freebusy(scope):
    service, _ = make_service(scopes=[scope])
    google_calls = []

    async def fake_freebusy_query(**kwargs):
        google_calls.append(kwargs)
        return {"calendars": {}}

    service._freebusy_query = fake_freebusy_query
    with pytest.raises(PermissionError):
        asyncio.run(
            service.query_freebusy(
                USER_ID,
                CONNECTION_ID,
                ["primary"],
                aware("2026-11-05T00:00:00Z"),
                aware("2026-11-06T00:00:00Z"),
                "UTC",
            )
        )

    assert not google_calls


def test_conflicts_detect_overlaps_but_not_touching_boundaries():
    events = [
        {
            "id": "one",
            "start": {"date_time": aware("2026-11-05T09:00:00Z")},
            "end": {"date_time": aware("2026-11-05T10:00:00Z")},
        },
        {
            "id": "two",
            "start": {"date_time": aware("2026-11-05T09:30:00Z")},
            "end": {"date_time": aware("2026-11-05T10:30:00Z")},
        },
        {
            "id": "touching",
            "start": {"date_time": aware("2026-11-05T10:30:00Z")},
            "end": {"date_time": aware("2026-11-05T11:00:00Z")},
        },
        {
            "id": "all-day",
            "start": {"date": date(2026, 11, 5)},
            "end": {"date": date(2026, 11, 6)},
        },
    ]

    conflicts = CalendarService.find_conflicts(
        events,
        ZoneInfo("UTC"),
    )

    assert [(item["event_id"], item["conflicting_event_id"])
            for item in conflicts] == [
        ("all-day", "one"),
        ("all-day", "two"),
        ("all-day", "touching"),
        ("one", "two"),
    ]


def test_google_api_error_is_not_swallowed():
    service, _ = make_service()

    async def failing_request(**_kwargs):
        raise GoogleAPIError(503, "Google API temporarily unavailable.", True)

    service._google_get = failing_request
    with pytest.raises(GoogleAPIError) as error:
        asyncio.run(service.list_events(USER_ID, CONNECTION_ID))
    assert error.value.status_code == 503


def test_google_api_client_retries_once_after_token_refresh(monkeypatch):
    token_calls = []
    request_calls = []

    class FakeCredentialService:
        def __init__(self, _db):
            pass

        async def get_valid_access_token(
            self,
            user_id,
            connection_id,
            rejected_access_token=None,
        ):
            token_calls.append((user_id, connection_id, rejected_access_token))
            return "fresh-token" if rejected_access_token else "old-token"

    class FakeDB:
        def commit(self):
            pass

        def rollback(self):
            pass

    async def fake_send_request(**kwargs):
        request_calls.append(kwargs)
        if len(request_calls) == 1:
            raise GoogleAPIError(401, "Google API request was rejected.")
        return {"items": []}

    monkeypatch.setattr(
        "app.integrations.google.api_client.OAuthCredentialService",
        FakeCredentialService,
    )
    client = GoogleAPIClient()
    client._send_request = fake_send_request
    result = asyncio.run(
        client.request(
            db=FakeDB(),
            user_id=str(USER_ID),
            connection_id=str(CONNECTION_ID),
            method="GET",
            url="https://www.googleapis.com/calendar/v3/calendars/primary/events",
        )
    )

    assert result == {"items": []}
    assert len(token_calls) == 2
    assert token_calls[1][2] == "old-token"
    assert len(request_calls) == 2
    assert all(call["method"] == "GET" for call in request_calls)


def test_calendar_service_only_uses_event_get_and_readonly_freebusy_post():
    service, _ = make_service()
    calls = []

    async def fake_api_request(**kwargs):
        calls.append(kwargs)
        return {"items": []} if kwargs["method"] == "GET" else {"calendars": {}}

    class FakeDB:
        def close(self):
            pass

    service.api_client.request = fake_api_request
    service_module_session = calendar_service_module.SessionLocal
    calendar_service_module.SessionLocal = lambda: FakeDB()
    try:
        asyncio.run(service.list_events(USER_ID, CONNECTION_ID))
        asyncio.run(
            service.query_freebusy(
                USER_ID,
                CONNECTION_ID,
                ["primary"],
                aware("2026-11-05T00:00:00Z"),
                aware("2026-11-06T00:00:00Z"),
                "UTC",
            )
        )
    finally:
        calendar_service_module.SessionLocal = service_module_session

    assert [(call["method"], call["url"]) for call in calls] == [
        (
            "GET",
            "https://www.googleapis.com/calendar/v3/"
            "calendars/primary/events",
        ),
        (
            "POST",
            "https://www.googleapis.com/calendar/v3/freeBusy",
        ),
    ]


def test_calendar_http_contracts_and_no_event_write_routes(monkeypatch):
    class FakeCalendarService:
        def __init__(self, _db):
            pass

        async def list_events(self, **_kwargs):
            return {
                "calendar_id": "primary",
                "events": [],
                "next_page_token": "next",
                "time_zone": "UTC",
            }

        async def get_event(self, **_kwargs):
            return {"id": "event-1"}

        async def get_today_schedule(self, **_kwargs):
            return {
                "calendar_id": "primary",
                "date": date(2026, 11, 5),
                "period": "day",
                "time_zone": "UTC",
                "events": [],
                "conflicts": [],
                "conflicts_complete": True,
                "next_page_token": None,
            }

        async def get_schedule(self, **_kwargs):
            return {
                "calendar_id": "primary",
                "date": date(2026, 11, 2),
                "period": "week",
                "time_zone": "UTC",
                "events": [],
                "conflicts": [],
                "conflicts_complete": True,
                "next_page_token": None,
            }

        async def query_freebusy(self, **kwargs):
            return {
                "time_min": kwargs["time_min"],
                "time_max": kwargs["time_max"],
                "time_zone": kwargs["time_zone"],
                "calendars": {"primary": {"busy": [], "errors": []}},
            }

    monkeypatch.setattr(calendar_endpoint, "CalendarService", FakeCalendarService)
    app.dependency_overrides[deps.get_current_user] = lambda: {
        "id": str(USER_ID),
    }
    app.dependency_overrides[get_db] = lambda: None
    try:
        with TestClient(app) as client:
            events = client.get(
                "/api/v1/calendar/events",
                params={"connection_id": str(CONNECTION_ID)},
            )
            detail = client.get(
                "/api/v1/calendar/events/event-1",
                params={"connection_id": str(CONNECTION_ID)},
            )
            today = client.get(
                "/api/v1/calendar/schedule/today",
                params={
                    "connection_id": str(CONNECTION_ID),
                    "time_zone": "UTC",
                },
            )
            schedule = client.get(
                "/api/v1/calendar/schedule",
                params={
                    "connection_id": str(CONNECTION_ID),
                    "date": "2026-11-05",
                    "period": "week",
                    "time_zone": "UTC",
                },
            )
            freebusy = client.post(
                "/api/v1/calendar/freebusy",
                json={
                    "connection_id": str(CONNECTION_ID),
                    "time_min": "2026-11-05T00:00:00Z",
                    "time_max": "2026-11-06T00:00:00Z",
                    "time_zone": "UTC",
                },
            )
            write_attempt = client.post(
                "/api/v1/calendar/events",
                json={"connection_id": str(CONNECTION_ID)},
            )

        assert events.status_code == 200
        assert events.json()["next_page_token"] == "next"
        assert events.headers["cache-control"] == "private, no-store"
        assert detail.status_code == 200
        assert today.status_code == 200
        assert schedule.status_code == 200
        assert freebusy.status_code == 200
        assert write_attempt.status_code == 405
    finally:
        app.dependency_overrides.clear()


def test_stale_google_authorization_failure_is_surfaced_without_provider_data(
    monkeypatch,
):
    class ForbiddenCalendarService:
        def __init__(self, _db):
            pass

        async def list_events(self, **_kwargs):
            raise GoogleAPIError(
                403,
                "invalid_scope access_token=do-not-expose",
            )

    monkeypatch.setattr(
        calendar_endpoint,
        "CalendarService",
        ForbiddenCalendarService,
    )
    app.dependency_overrides[deps.get_current_user] = lambda: {
        "id": str(USER_ID),
    }
    app.dependency_overrides[get_db] = lambda: None
    try:
        with TestClient(app) as client:
            response = client.get(
                "/api/v1/calendar/events",
                params={"connection_id": str(CONNECTION_ID)},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 403
    assert response.json()["detail"] == (
        "Google denied access to this Calendar resource."
    )
    assert "do-not-expose" not in response.text


def test_freebusy_body_requires_uuid_and_aware_datetimes():
    from app.schemas.calendar import FreeBusyRequest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        FreeBusyRequest.model_validate({
            "connection_id": "not-a-uuid",
            "time_min": "2026-11-05T09:00:00Z",
            "time_max": "2026-11-05T10:00:00Z",
            "time_zone": "UTC",
        })
    with pytest.raises(ValidationError):
        FreeBusyRequest.model_validate({
            "connection_id": str(CONNECTION_ID),
            "time_min": "2026-11-05T09:00:00",
            "time_max": "2026-11-05T10:00:00",
            "time_zone": "UTC",
        })


def test_invalid_connection_uuid_rejected_by_route():
    app.dependency_overrides[deps.get_current_user] = lambda: {
        "id": str(USER_ID),
    }
    app.dependency_overrides[get_db] = lambda: None
    try:
        with TestClient(app) as client:
            response = client.get(
                "/api/v1/calendar/events",
                params={"connection_id": "not-a-uuid"},
            )
        assert response.status_code == 422
    finally:
        app.dependency_overrides.clear()
