from app.core.config import Settings, get_settings, settings


CALENDAR_FLAG_NAMES = (
    "CALENDAR_CREATE_EVENT_ENABLED",
    "CALENDAR_UPDATE_EVENT_ENABLED",
    "CALENDAR_DELETE_EVENT_ENABLED",
)


def test_calendar_feature_flags_load_from_named_environment_values(monkeypatch):
    values = {
        "CALENDAR_CREATE_EVENT_ENABLED": "true",
        "CALENDAR_UPDATE_EVENT_ENABLED": "TRUE",
        "CALENDAR_DELETE_EVENT_ENABLED": "false",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    loaded = Settings(_env_file=None)

    assert loaded.CALENDAR_CREATE_EVENT_ENABLED is True
    assert loaded.CALENDAR_UPDATE_EVENT_ENABLED is True
    assert loaded.CALENDAR_DELETE_EVENT_ENABLED is False


def test_calendar_feature_flags_are_cached_for_the_process(monkeypatch):
    cached = get_settings()
    original_values = {
        name: getattr(cached, name)
        for name in CALENDAR_FLAG_NAMES
    }
    for name, value in original_values.items():
        monkeypatch.setenv(name, "false" if value else "true")

    assert get_settings() is cached
    assert settings is cached
    assert {
        name: getattr(get_settings(), name)
        for name in CALENDAR_FLAG_NAMES
    } == original_values
