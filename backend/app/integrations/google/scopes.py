CALENDAR_EVENTS_READ_SCOPE = (
    "https://www.googleapis.com/auth/calendar.events.readonly"
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

CALENDAR_FREEBUSY_SCOPES = frozenset({
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar",
    CALENDAR_FREEBUSY_SCOPE,
    "https://www.googleapis.com/auth/calendar.freebusy",
})
