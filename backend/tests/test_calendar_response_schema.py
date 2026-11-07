
from app.schemas.calendar import CalendarEvent
from app.services.calendar_service import CalendarService


def test_calendar_event_preserves_google_etag():
    raw_event = {
        "id": "event-123",
        "etag": '"v1"',
        "summary": "Test Calendar Event",
    }

    normalized = CalendarService._normalize_event(raw_event)
    event = CalendarEvent.model_validate(normalized)

    assert event.id == "event-123"
    assert event.etag == '"v1"'


def test_calendar_event_allows_missing_etag():
    event = CalendarEvent.model_validate({
        "id": "event-456",
        "summary": "Test Event",
    })

    assert event.id == "event-456"
    assert event.etag is None
