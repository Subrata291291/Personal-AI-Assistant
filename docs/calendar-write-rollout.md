# Calendar write rollout

Calendar writes require all of the following:

1. Apply the additive migration through the normal deployment process. The
   resulting Alembic head is `c9f42a71e6b3`; this migration binds one-time
   OAuth consent state to an owned Google connection.
2. The user grants the incremental
   `https://www.googleapis.com/auth/calendar.events` scope for a selected
   connected Google account in Connected Apps.
3. Configure each operation independently in the target environment:
   `CALENDAR_CREATE_EVENT_ENABLED`, `CALENDAR_UPDATE_EVENT_ENABLED`, and
   `CALENDAR_DELETE_EVENT_ENABLED`. Set only the flag for an operation that
   has passed its rollout checks; leave the other two `false`. Each defaults
   to `false`, and changing one flag does not authorize either of the other
   operations.

The write-consent flow requests only the Calendar events write scope and
verifies that the callback belongs to the selected Google account. Actual
granted scopes are persisted and reported separately from feature-flag
availability. OAuth disconnect and application enable/disable preferences
remain independent.

Calendar discovery is read-only and uses
`https://www.googleapis.com/auth/calendar.calendarlist.readonly`. Existing
connections without this grant cannot browse or select additional calendars;
they must grant it through the existing Calendar authorization flow. This
scope does not grant event-write permission or enable any write operation.

Every write is prepared as a user-bound approval proposal. The user must
approve and then separately execute it. Updates and deletes are bound to a
live event snapshot and ETag, use conditional requests, and are not retried
automatically. Unconfirmed outcomes remain uncertain until reconciled.

Activation sequence for each operation:

1. Deploy and verify the application's Calendar operation migration and
   required database permissions through the normal release process.
2. Verify the selected account's Calendar events write grant and the
   ownership-bound consent flow.
3. Run that operation's mocked approval, flag-enforcement, conditional-write,
   and provider read-back tests in the target release.
4. In the target environment's normal configuration process, set only the
   matching operation flag to `true`; keep the other operation flags `false`
   unless separately approved and verified.
5. Confirm the UI reports that operation as available only after the backend
   reports it, and monitor the operation journal and reconciliation outcomes.

Do not enable a write flag before these checks pass. No migration, flag
change, provider mutation, or deployment is performed by this code change.
