# Calendar write rollout

Calendar writes require all of the following:

1. Apply the additive migration through the normal deployment process. The
   resulting Alembic head is `c9f42a71e6b3`; this migration binds one-time
   OAuth consent state to an owned Google connection.
2. The user grants the incremental
   `https://www.googleapis.com/auth/calendar.events` scope for a selected
   connected Google account in Connected Apps.
3. Enable only the operation flags intended for that environment:
   `CALENDAR_CREATE_EVENT_ENABLED`, `CALENDAR_UPDATE_EVENT_ENABLED`, and
   `CALENDAR_DELETE_EVENT_ENABLED`. Each defaults to `false`.

The write-consent flow requests only the Calendar events write scope and
verifies that the callback belongs to the selected Google account. Actual
granted scopes are persisted and reported separately from feature-flag
availability. OAuth disconnect and application enable/disable preferences
remain independent.

Every write is prepared as a user-bound approval proposal. The user must
approve and then separately execute it. Updates and deletes are bound to a
live event snapshot and ETag, use conditional requests, and are not retried
automatically. Unconfirmed outcomes remain uncertain until reconciled.

Do not enable a write flag before the migration is deployed and the
environment's OAuth and approval-flow tests have passed. No migration is run
by this change.
