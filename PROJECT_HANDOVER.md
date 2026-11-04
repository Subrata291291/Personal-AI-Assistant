Personal AI Assistant --- Project Handover
Purpose: This file is the persistent handover for continuing this
project in a new Codex/VS Code chat or after signing in with a
different GitHub account.

Workspace: C:\Users\ASUS\Desktop\personal_ai_assistant
Repository: Subrata291291/Personal-AI-Assistant
Last handover prepared: 2026-11-04

1. Instructions for the next AI/Codex session
Read this file completely before making changes.
1. Start by running git status --short from the project root.
2. The worktree contains accumulated modified and untracked files.
   Preserve all of them. Never run git reset, git clean, broad
   reverts, or overwrite existing work.
3. Inspect the current implementation before editing. This document is
   a handover, not proof that every listed file or behavior is still
   unchanged.
4. Work in small, targeted steps. Explain the root cause before
   applying a fix.
5. Do not create automated test files or a test suite. Use existing
   verify_*.py scripts and focused mocked/manual verification.
6. Do not change database schemas or add migrations unless a real
   requirement is established and explained first.
7. Do not install unnecessary dependencies.
8. Never perform live Gmail write operations. Do not send, trash,
   archive, reply, forward, or create live Gmail drafts during
   verification. Do not enable additional Gmail write actions without
   explicit user approval.
9. Preserve authentication, multi-user isolation, RLS assumptions,
   explicit ownership checks, OAuth token encryption, idempotency,
   approval semantics, and provider fallback/error classification.
10. Never log or expose .env values, OAuth tokens, API keys,
    credentials, or sensitive email contents.
11. Keep diagnostics useful but redacted. Avoid broad silent exception
    handling.
12. Distinguish mocked verification from real browser, provider, Gmail,
    and Supabase verification. Never claim a live check passed when only
    mocks were used.
13. Do not commit changes unless explicitly asked.
14. Do not assume the current database migration level from local
    migration files. Inspect safely if database state matters.
2. Project overview
A multi-user Personal AI Assistant built from scratch.
Main stack
Backend - Python 3.12 - FastAPI - Pydantic - SQLAlchemy and
Alembic - Supabase Auth and PostgreSQL - LangGraph - Google OAuth and
Google APIs

Frontend - React 19 - Vite - JavaScript - TanStack Query - Supabase
JS - Tailwind CSS

AI and retrieval - Groq - OpenRouter - Gemini - OpenAI - Gemini
embeddings - PostgreSQL with pgvector
Main folder structure
personal_ai_assistant/
├── backend/
│   ├── app/
│   │   ├── api/
│   │   ├── core/
│   │   ├── db/
│   │   ├── integrations/
│   │   ├── llm/
│   │   ├── memory/
│   │   ├── repositories/
│   │   ├── services/
│   │   └── workflows/
│   ├── db_migrations/
│   │   └── versions/
│   └── ...
├── frontend/
│   ├── src/
│   │   ├── components/
│   │   ├── context/
│   │   ├── pages/
│   │   ├── services/
│   │   └── utils/
│   └── ...
├── README.md
├── .gitignore
└── PROJECT_HANDOVER.md
The exact current tree must be checked before editing.
3. Implemented capabilities
- Supabase-backed signup/login and authenticated dashboard.
- Multi-user conversation persistence and history restoration.
- Chat request idempotency, leases, retry/error handling.
- Conditional LangGraph routing for general chat and Gmail.
- Persistent user-scoped memory with retrieval, classification,
  deduplication, conflict handling, and review workflow.
- LLM provider fallback and structured error handling.
- Gmail account connection and management.
- Gmail inbox, folders, search, details, attachments.
- Gmail read/search/thread-summary Assistant intents.
- Editable email draft previews.
- Existing Gmail API write endpoints and a constrained
  approval/execution path for Gmail send.
- Gmail HTML sanitization and sandboxed rendering.
- Assistant Markdown/GFM rendering.
- Provider completion metadata and incomplete-response notices.
- Request correlation and timing instrumentation.
- Gmail list metadata retrieval, bounded concurrency, frontend caching
  and debounced search.
4. Gmail integration status and safety
Existing Gmail API capabilities
The Gmail service/API has coverage for: - List/folders/search. - Full
message details and attachments. - Send. - Trash, restore and archive. -
Reply, reply-all and forward. - Draft creation/save flow.

These API operations exist separately from the Assistant's constrained
planner and approval executor. Do not assume every operation is enabled
through the Assistant.
Assistant Gmail behavior
- Gmail requests route to a Gmail workflow branch.
- Common intents, including important-email summaries, have
  deterministic routing.
- Other requests may use a structured planner.
- Gmail plans are validated; identifiers are checked against available
  sources; result context is bounded.
- Editable draft previews are distinct from saved Gmail drafts.
- The approval proposal/API/UI path exists in the current accumulated
  worktree.
- The Gmail executor is limited to approved gmail.send actions.
- Approval and execution are separate. Approval alone must not perform
  an external action.
- Uncertain send outcomes must not be blindly retried.
- Never perform live Gmail writes during verification.
Email and HTML security
Email content is untrusted. - HTML is sanitized before rendering. -
Scripts, forms, frames, event attributes and unsafe links are removed. -
Accepted links are restricted to HTTP(S) and mailto: and configured
for safe new-tab navigation. - The email renderer retains iframe sandbox
protections. - Do not remove sandbox protections or allow email scripts
to execute. - Do not expose application cookies or credentials to email
content.
5. LangGraph workflow
The graph begins at intent_router.
- General requests route to memory retrieval and then the general LLM
  node.
- Gmail requests route to the Gmail agent.
- Google Drive, Calendar, Contacts, and task routes currently return
  an explicit not-connected/unavailable response. They are
  placeholders, not implemented integrations.
- Gmail planner output is schema-validated.
- Truncated planner output is not executed.
- The chat endpoint currently calls synchronous chat_graph.invoke.
- Gmail async operations are bridged with asyncio.run in the
  workflow.
Do not convert the entire workflow to async without tracing and
measuring the consequences.

LangGraph checkpointing uses an in-memory saver. Durable conversation
and request state use database/Supabase persistence.
6. Memory architecture
- PostgreSQL/pgvector memory records use 768-dimensional Gemini
  embeddings.
- General-chat requests retrieve up to five relevant memories above
  the configured similarity threshold.
- Retrieved memories are framed as untrusted data in the prompt.
- Retrieval failure is logged and chat continues without memory.
- Memory processing can classify, policy-check, deduplicate, resolve
  conflicts, and send ambiguous changes for user review.
- Automatic memory storage respects the user's memory_enabled
  preference.
- Automatic storage and retrieval are separate concerns; do not assume
  disabling storage disables retrieval.
- Embeddings use Gemini's embedding API, so embedding operations
  depend on Gemini independently of the chat-generation provider.
7. LLM providers and response limits
Provider priority:

Groq → OpenRouter → Gemini → OpenAI
- Groq is attempted once per request.
- Fallback is for classified transient failures.
- A successful but token-limited response must not trigger provider
  fallback.
- Provider adapters preserve usage and finish-reason metadata.
- General replies request up to 1,800 output tokens.
- Gmail summaries request up to 1,600 output tokens.
- Provider output ceilings are configurable and default to 2,048.
- Assistant message metadata stores completion status, finish reason
  and token usage.
- The UI warns when the provider reports an output-limit stop.
- Automatic response continuation is not implemented.
Never inspect, paste, log, or commit .env values.
8. Authentication, multi-user isolation and database
- Supabase Auth validates bearer tokens.
- Authenticated user identity scopes conversations, memories,
  connectors and action data.
- Some SQLAlchemy repository paths use a trusted database role that
  may bypass RLS. Those paths must retain explicit
  user/conversation/connection ownership filters.
- Google OAuth tokens are encrypted before persistence and decrypted
  only for provider calls.
- Credentials are tied to both connection and user.
- Gmail requests validate connection ownership and required scopes.
- Approval proposals are user-scoped, immutable in their action
  parameters, expire, and use conditional state transitions.
- Email bodies, attachments, model output and memory contents remain
  untrusted.
Application migration history includes: - pa_conversations -
pa_conversation_messages - pa_chat_requests - pa_user_settings -
pa_user_connections - pa_oauth_credentials - pa_oauth_states -
pa_memories - pa_memory_reviews

Supabase auth.users is external to these application migrations.

Current approval records are stored in conversation-message JSON
metadata. There is no separate action-approval table or migration.

Do not infer the live database's migration level from local files alone.
Do not alter existing user data or tables casually.
9. Performance work completed
Gmail
- Inbox listing uses metadata retrieval rather than fetching all
  bodies.
- Independent message metadata reads use bounded concurrency (maximum
  five).
- Summary operations bound the number and text of messages passed to
  the model.
- Gmail results are paginated.
- Frontend uses useInfiniteQuery.
- TanStack Query uses user/account-specific keys and mutation
  invalidation.
- Default query settings include a two-minute staleTime and
  30-minute gcTime.
- Gmail search is debounced.
- Message details load separately from inbox lists.
Assistant and backend timing
Request correlation/timing instrumentation covers authentication, graph
nodes, embeddings, provider calls and Google API requests. Logs must not
include tokens or email content.

Prior mocked five-run median timings: - Listing 20 messages: 667.13 ms →
159.46 ms. - Fetching 10 summary details: 311.66 ms → 64.19 ms.

These are mocked timings, not live Gmail or end-to-end timings. API
request counts did not change; the improvement came from bounded
concurrency and connection reuse.

Streaming was not introduced. There are no reliable incremental graph
stages wired to SSE. Do not weaken idempotency, lease or persistence
behavior to add streaming.
10. Unresolved issues and verification limits
1. The original Assistant failure shown in the user's screenshot has
   not been definitively reproduced. Safe redacted traceback
   diagnostics were added, but the original failing stage remains
   unconfirmed.
2. The original schedule truncation's exact provider finish reason was
   not captured. The previous 500-token general output limit was likely
   relevant, but do not claim it definitively caused that specific
   screenshot.
3. Provider output limits can still be reached. Unknown finish reasons
   must remain unknown; do not silently label a response complete.
4. No live provider reproduction, authenticated browser verification,
   or live Gmail verification has been completed for all recent
   changes.
5. Gmail custom operation-marker preservation needs staging
   verification before relying on it for draft-save reconciliation.
6. The approval implementation in the dirty worktree needs end-to-end
   review and verification:
   - Proposal creation.
   - User/account ownership.
   - Scope checks.
   - Expiry.
   - Approve/reject.
   - Atomic execution claim.
   - Duplicate and concurrent execution.
   - Timeout/uncertain-state persistence.
   - UI/API serialization and conversation restoration.
7. Verify the approval flow with mocked Gmail/database dependencies
   only. Never send a real email.
8. The action schema declares other connector action types, but the
   executor supports only gmail.send. Do not treat other action types
   as implemented.
9. Direct Gmail write endpoints exist separately from the Assistant's
   approval executor. Confirm intended UX/gating before changing them.
   Do not broaden them.
10. Drive, Calendar, Contacts, task/reminder integrations and streaming
    are not implemented.
11. Markdown and HTML sanitizer implementations exist, but browser
    visual behavior in both themes and on mobile still needs
    verification.
12. The frontend build has previously reported a large-chunk warning.
    Lint has reported existing warnings.
11. Current worktree preservation
The last handover reported accumulated modified and untracked files. The
exact state must be rechecked using git status --short.

Previously modified backend areas included: - API/auth: deps.py,
chat.py, messages.py, main.py. - Configuration/database:
config.py, session.py, .env.example. - Google/Gmail:
api_client.py, email_parser.py, message_builder.py,
gmail_service.py, gmail_tools.py, gmail_agent_nodes.py,
email_draft.py, gmail_agent.py. - LLM/workflows: fallback.py,
router.py, schemas.py, provider adapters, chat_graph.py,
chat_nodes.py, chat_state.py, chat_service.py. - Memory:
embedding_service.py. - Persistence: message_service.py.

Previously modified frontend areas included: - package.json,
package-lock.json. - EmailDraftCard.jsx, DashboardLayout.jsx,
AuthContext.jsx, api.js, Assistant.jsx, Connectors.jsx,
Dashboard.jsx, Gmail.jsx, conversationService.js.

Previously untracked additions included: - Approval:
action_approvals.py, action_approval_repository.py,
action_approval.py, action_approval_service.py,
gmail_action_executor.py, ActionApprovalCard.jsx,
actionApprovalService.js. - Diagnostics/account selection:
error_logging.py, request_metrics.py, gmailAccountSelection.js. -
Rendering/safety: AssistantMarkdown.jsx, sanitizeEmailHtml.js.

This list may be stale. Always inspect the actual worktree. Preserve
every current modified and untracked file, including files not listed
here.
12. Exact next development task
Complete non-destructive end-to-end verification of the current Gmail
action-approval flow before adding more write behavior.

Trace: 1. Proposal creation. 2. Account and scope validation. 3. Pending
UI state. 4. Approval and rejection. 5. Atomic execution claim. 6.
Executor outcome persistence. 7. Duplicate/concurrent execution
prevention. 8. Timeout and uncertain-state handling. 9. Conversation
restoration and UI/API serialization.

Use mocked Gmail and database dependencies. Confirm that
duplicate/concurrent execution cannot cause two sends and uncertain
outcomes require manual resolution. Do not perform live Gmail writes or
create migrations unless an actual schema requirement is established.

After that: - Perform authenticated browser verification of Assistant
Markdown rendering and truncation notices. - Verify Gmail HTML link
navigation safely. - Capture real provider finish-reason metadata if the
original schedule cutoff can be reproduced. - Only then begin Google
Drive Agent development.
13. Suggested first prompt for a new Codex chat
Paste this after opening the same project folder with the new GitHub
account:
Read PROJECT_HANDOVER.md completely before making changes.

This is an existing project with accumulated modified and untracked work.
First run git status --short and report the current state. Do not reset,
clean, revert, overwrite, or discard any existing changes.

Our next task is to complete non-destructive end-to-end verification of
the existing Gmail action-approval flow. Inspect the current code before
making assumptions. Trace proposal creation, ownership and scope checks,
approval/rejection, atomic execution claim, duplicate/concurrent execution,
uncertain outcomes, persistence, and conversation restoration.

Use mocked Gmail and database dependencies only. Never perform live Gmail
writes or send a real email. Do not add automated test files, migrations,
or dependencies. Preserve authentication, user/account isolation, RLS
assumptions, OAuth encryption, idempotency, approval semantics, and all
existing worktree changes.

Start by reporting the actual git status and a concise verification plan.
Do not edit files until the current implementation and risks have been
reviewed and explained.
14. Local development commands
Backend --- PowerShell terminal 1
cd C:\Users\ASUS\Desktop\personal_ai_assistant\backend
.\.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
Frontend --- PowerShell terminal 2
cd C:\Users\ASUS\Desktop\personal_ai_assistant\frontend
npm run dev
- Frontend: http://localhost:5173
- Backend: http://127.0.0.1:8000
- API docs: http://127.0.0.1:8000/docs
Keep .env private. Never paste secrets into chat or commit them.