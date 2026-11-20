# Phase 30 — Google Integrations Grounding Test Plan

## Scope

Audit Gmail, Google Calendar, and Google Contacts with one rule:

> Every factual claim about a user's Google data must be traceable to authoritative data retrieved from the correct connected Google account.

Automated verification uses deterministic mocks and synthetic fixtures only. No live Google reads, writes, approvals, or OAuth changes are part of this phase. Live read verification would require explicit user permission and is reported separately if performed.

## Preservation constraints

- Preserve Phase 26–29 behavior, including Calendar availability/timezone handling and cross-domain pending-state restoration.
- Preserve existing Gmail, Calendar, Contacts, Memory, RAG, LangGraph, MCP, LangSmith, approval, and streaming behavior.
- Preserve provider priority: Groq → OpenRouter → Gemini → OpenAI.
- Do not enable Google Drive.
- Do not modify OAuth scopes, `.env`, production credentials, production data, or unrelated working-tree changes.
- No staging, commit, push, deploy, `git reset --hard`, `git clean`, `git add .`, or `git add -A`.

## Provenance chain under test

For every scenario:

`Authenticated user → Connected account → Google API request → API response → Normalized application data → LangGraph state → Final assistant response`

Required evidence by field:

| Claim | Authoritative source |
|---|---|
| Email subject/sender/recipient/date/snippet/body/message ID/thread ID | Gmail `users.messages.get` / `users.threads.get`, headers and `internalDate` |
| Calendar event ID, summary, start/end, timezone, recurrence, status, attendees, calendar ID | Calendar Events API resource |
| Availability/free intervals | Calendar FreeBusy API response |
| Contact resource name, name, emails, phones, organizations, titles, addresses | People API `people.get` / validated `people.connections.list` results |

Search snippets or model-generated text are not sufficient evidence for full-resource field claims.

## Question matrix

### Gmail

1. Check my last email.
2. Show my latest five emails.
3. Show all emails received today.
4. Show unread emails.
5. Search emails from a specific sender.
6. Find emails with a specific subject.
7. Summarize a particular email.
8. What did the sender ask me to do?
9. Show the latest reply in a thread.
10. What is the date of the latest email?
11. Do I have an email from a named person?
12. Show emails within a date range.
13. No matching emails.
14. Gmail API error.
15. Follow-up about a previously retrieved email.
16. Switch from Calendar/Contacts to Gmail during a pending clarification.

Validate account identity, message IDs, thread IDs, sender, recipients, subject, timestamps, snippets, body, labels, and source provenance.

### Calendar

1. What is my next event?
2. Show today's events.
3. Show tomorrow's schedule.
4. Am I free tomorrow?
5. Am I free between 11 AM and 2 PM?
6. Why am I busy during that interval?
7. Show all events on a specific date.
8. Find an event by title.
9. Find an event using a partial title.
10. Find an event using a title and date.
11. What time does a specific event start?
12. What is the event's timezone?
13. Show recurring events.
14. No events.
15. Calendar API error.
16. Follow-up about a previously retrieved event.
17. Switch from Gmail/Contacts to Calendar during a pending clarification.

Include exact prior failure scenarios: `gym time for`, `gym session`, `lunch`, `November 15, 2026`, `Asia/Kolkata`, and event-detail messages containing `Title`, `Date`, `Start`, `End`, `Timezone`, and `Calendar` labels.

Validate event IDs, calendar IDs, summary, description, start/end, timezone, recurrence, status, attendees, free/busy intervals, and source provenance.

### Contacts

1. Show my contacts.
2. Search for a contact by name.
3. Find a contact by email.
4. Show a contact's phone number.
5. Show a contact's organization.
6. Show a contact's job title.
7. Show a contact's address.
8. Find contacts with similar names.
9. Multiple matching contacts.
10. No matching contact.
11. People API error.
12. Follow-up about a previously retrieved contact.
13. Switch between Contacts, Gmail, and Calendar while a clarification is pending.

Validate resource names, display names, emails, phones, organizations, titles, addresses, returned fields, and provenance.

## No-hallucination matrix

Apply to all three integrations:

1. Valid records returned.
2. Empty result.
3. Partial records.
4. Missing optional fields.
5. Malformed records.
6. API error.
7. API timeout.
8. Connected account changes.
9. Wrong connection ID.
10. Authenticated user changes.
11. Conversation history contains unrelated personal data.
12. LLM injects unsupported facts.
13. Previous assistant answer contains incorrect data.
14. User asks a follow-up referring to an earlier result.
15. Streaming fails after partial output.

Expected behavior:

- Empty result: explicitly report that no matching records were found.
- API failure/timeout: report that retrieval failed; never invent data.
- Missing field: state that the field is unavailable.
- Ambiguous result: ask a focused clarification.
- Unsupported LLM claims: strip, block, or mark unverified; never present as Google-backed fact.
- Wrong user/connection/account: fail closed with no cross-account data leakage.

## Cross-domain sequences

- Calendar clarification → Gmail request → Calendar follow-up.
- Gmail clarification → Contacts request → Gmail follow-up.
- Contacts clarification → Calendar request → Contacts follow-up.
- Calendar event lookup → general question → Calendar follow-up.
- Gmail search → account switch → Gmail follow-up.
- Contact lookup → new contact lookup.
- Calendar availability → ask why busy → retrieve actual events.

Verify pending-state scope: user, conversation, connection, operation, expiry; fresh intent wins; genuine continuation restores correctly; no stale hijacking or leakage.

## Validation layers

Tests must assert the final answer after LangGraph execution, LLM synthesis, post-processing, persistence, SSE streaming, and frontend rendering where applicable. Intermediate tool/state assertions alone are insufficient.

When a fabricated personal-data claim is injected into LLM output, the final user-visible answer must not present it as verified Google data.

## Test organization

Add a dedicated suite under `backend/tests/`, plus targeted additions to existing domain suites only where a fix requires them. Use shared deterministic harnesses with:

- authoritative synthetic API fixtures,
- call spies for Google API request arguments,
- explicit user/connection IDs,
- provenance capture,
- final-response claim checks,
- invoke and streaming variants.

## Exit criteria

Production-ready only if all supported scenarios pass, every personal-data claim is source-backed, no fabricated record passes, empty/failed responses never invent data, isolation and cross-domain tests pass, streaming and non-streaming guarantees match, approval safeguards remain intact, full backend suite passes, frontend suite passes where applicable, and no unrelated feature regresses.

Otherwise report **NOT YET PRODUCTION-READY** with explicit blockers.
