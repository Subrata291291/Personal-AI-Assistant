CALENDAR_EVENTS_READ_SCOPE = (
    "https://www.googleapis.com/auth/calendar.events.readonly"
)
CALENDAR_LIST_READ_SCOPE = (
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly"
)
CALENDAR_EVENTS_WRITE_SCOPE = (
    "https://www.googleapis.com/auth/calendar.events"
)
CALENDAR_FREEBUSY_SCOPE = (
    "https://www.googleapis.com/auth/calendar.events.freebusy"
)

CALENDAR_EVENT_READ_SCOPES = frozenset({
    CALENDAR_EVENTS_READ_SCOPE,
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar",
    # Keep event reads working for users with the previous Calendar grant.
    "https://www.googleapis.com/auth/calendar.events",
})

CALENDAR_LIST_READ_SCOPES = frozenset({
    CALENDAR_LIST_READ_SCOPE,
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar",
})

CALENDAR_FREEBUSY_SCOPES = frozenset({
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar",
    CALENDAR_FREEBUSY_SCOPE,
    "https://www.googleapis.com/auth/calendar.freebusy",
})

CONTACTS_READ_SCOPE = (
    "https://www.googleapis.com/auth/contacts.readonly"
)
CONTACTS_WRITE_SCOPE = (
    "https://www.googleapis.com/auth/contacts"
)

DRIVE_READ_SCOPE = (
    "https://www.googleapis.com/auth/drive.readonly"
)
# Least-privilege Drive scope: only files created or explicitly opened by
# this app through Google Picker. The application only reads these files.
DRIVE_FILE_SCOPE = (
    "https://www.googleapis.com/auth/drive.file"
)
# Backwards-compatible name used by the earlier Drive capability foundation.
DRIVE_WRITE_SCOPE = DRIVE_FILE_SCOPE

DRIVE_READ_SCOPES = frozenset({
    DRIVE_READ_SCOPE,
    "https://www.googleapis.com/auth/drive",
})

DRIVE_FILE_SCOPES = frozenset({
    DRIVE_FILE_SCOPE,
})

DRIVE_WRITE_SCOPES = frozenset({
    DRIVE_FILE_SCOPE,
    "https://www.googleapis.com/auth/drive",
})

# Scopes that permit reading files the user explicitly selected for this app.
# drive.file is the MVP scope; broader read-only grants are accepted when the
# user already consented to them, but the application never requests them for
# the contracts feature.
DRIVE_CONTRACTS_READ_SCOPES = frozenset(
    DRIVE_FILE_SCOPES | DRIVE_READ_SCOPES
)
