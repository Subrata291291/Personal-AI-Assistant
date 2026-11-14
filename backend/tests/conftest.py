"""Shared test configuration.

These fixtures isolate tests from developer-specific ``.env`` values. The
application settings object is a process-wide singleton loaded from the root
``.env`` file, so a test that asserts default-off behaviour must pin the
relevant flags explicitly instead of relying on the machine it runs on.

The fixtures only mutate settings for the duration of a test; ``monkeypatch``
restores the original values afterwards. Production configuration and the
defaults in ``app.core.config`` are unchanged.
"""

import pytest

from app.core.config import settings


CALENDAR_WRITE_FLAG_NAMES = (
    "CALENDAR_CREATE_EVENT_ENABLED",
    "CALENDAR_UPDATE_EVENT_ENABLED",
    "CALENDAR_DELETE_EVENT_ENABLED",
)


@pytest.fixture
def calendar_write_flags_disabled(monkeypatch):
    """Pin all Calendar write feature flags to False for the current test."""
    for name in CALENDAR_WRITE_FLAG_NAMES:
        monkeypatch.setattr(settings, name, False)
    return settings
