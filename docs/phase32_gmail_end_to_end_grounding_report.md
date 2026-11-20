# Phase 32 — Independent Gmail End-to-End Grounding Audit

Date: 2026-11-20
Status: **AUTOMATED GATES PASS — NOT YET PRODUCTION-READY**

---

## 1. Executive verdict

The Phase 32 audit is complete for deterministic, mock-based verification. Every user-visible Gmail factual claim path was traced from routing → validated plan → read-only tool invocation → authoritative message record → rendered answer, and defects found on that path were reproduced, fixed, and retested.

**Automated result:** backend `1498 passed, 13 warnings in 78.93s`; frontend `33 test files passed, 388 tests passed`; lint `0 errors`; production build succeeded.

**Final verdict:** **NOT YET PRODUCTION-READY.** Gate A (deterministic automation) passes. Gate B (local manual browser), Gate C (live authenticated Gmail), and Gate D (production deployment) were **NOT PERFORMED** and are explicitly out of scope without authorization. No live Google data, live credentials, live LLM calls, OAuth/consent changes, Gmail/Calendar/Contacts mutations, staging, commit, push, or deployment occurred.

---

## 2. Scope and rules audited

The audit covered five linked surfaces:

1. Gmail retrieval, counters, pagination, truncation, and bounded read ceiling.
2. Claim-level grounding: message ID, sender, subject, date, summary, action, deadline, attachments — per message.
3. Completeness model: `complete`, `partial`, `failed`, `cancelled`, plus `output_limited` / `finish_reason`.
4. True token streaming, mid-stream failure, and SSE persistence parity.
5. Frontend rendering of notices, progress, replay, duplicates, and cancellation.

Invariants preserved throughout:

- `MAX_GMAIL_READ_RESULTS = 50` unchanged.
- `SUMMARY_BATCH_SIZE = 5`, `SUMMARY_BATCH_MAX_TOKENS = 900`, `LONG_SUMMARY_MIN_MESSAGES = 3` unchanged.
- Provider priority `Groq → OpenRouter → Gemini → OpenAI` untouched.
- OAuth scopes/consent, `.env`, credentials, API keys, production secrets untouched.
- Drive status, approval workflows, and isolation safeguards untouched.
- No Gmail/Calendar/Contacts mutation; deterministic synthetic fixtures only.
- No `git add .`, `git add -A`, `git reset --hard`, `git clean`, staging, commit, push, or deploy.

---

## 3. Gate A — deterministic automated verification (PERFORMED)

Gate A is a full deterministic, offline audit using synthetic Gmail fixtures and recording doubles. All external boundaries are mocked: `GmailService`, `SessionLocal`, `llm_router`, SSE streams, and browser DOM.

| Check | Result |
| --- | --- |
| 42-question end-to-end matrix | **PASS** — 44 tests (42 cases + 2 structural) |
| Gmail grounding regressions | **PASS** — 12 tests |
| Full backend suite | **PASS** — 1498 tests |
| Full frontend suite | **PASS** — 388 tests |
| Lint | **PASS** — 0 errors (warnings only) |
| Production build | **PASS** — 423 modules, built in 832 ms |

Gate A is **PASS**. Gates B–D remain **NOT PERFORMED**.

---

## 4. Gate B — local manual browser verification (NOT PERFORMED)

**NOT PERFORMED.** No in-app browser session, screenshot review, or human-observed interaction with the running frontend was executed. Automated DOM tests (`Assistant.test.jsx`, `conversationService.test.js`) cover the rendering contracts, but tests are not a substitute for scripted manual verification. Required before production: sign in locally, run each matrix question, and visually confirm notices, progress, replay, and no duplicate bubbles.

---

## 5. Gate C — live authenticated Gmail verification (NOT PERFORMED)

**NOT PERFORMED.** No live Google API call, real OAuth token, or production Gmail account was used. All evidence comes from synthetic messages. Required before production: an explicitly authorized, read-only live account; verify each matrix question against real message IDs, senders, dates, and attachments; confirm no scope or consent changes are needed and no mutation occurs.

---

## 6. Gate D — production deployment verification (NOT PERFORMED)

**NOT PERFORMED.** No build was deployed, no production environment was touched, and no production rollout/smoke test was run. The local `npm run build` result is a compile check only. Required before production: staged rollout, streaming/SSE smoke test, and monitoring of completeness/grounding metadata in production logs.

---

## 7. Evidence model and codes

Every claim is graded against message-level authoritative data:

| Code | Meaning |
| --- | --- |
| `MSG-ID` | Result cites a concrete Gmail message ID from the retrieved fixture. |
| `HDR` | Sender / subject / date comes from parsed Gmail headers, not model text. |
| `BODY` | Summary/action/dedeadline is validated against that message's own body. |
| `ATT` | Attachment claim is supported by authoritative attachment metadata. |
| `CNT` | `requested/candidate/retrieved/processed/failed/remaining` counters are recomputed after capping. |
| `CMP` | Completeness status distinguishes complete/partial/failed/cancelled and output-limited. |
| `AUTH` | Every service call carries the expected authenticated `user_id` and `connection_id`. |
| `NONE` | Claim is absent from authoritative data and remains explicitly unavailable. |

Route-only assertions were explicitly rejected as sufficient; the matrix asserts plan arguments, invoked tool methods, call arguments, identity, and returned message-level sources.

---

## 8. 42-question Gmail matrix

The matrix is codified in `backend/tests/test_gmail_end_to_end_matrix_phase32.py`. Each case asserts domain, plan intent + arguments, invoked read-only tools, query/folder/limit arguments, authenticated user/connection, and message-level evidence. Representative authoritative outcomes:

| # | Question | Domain | Plan / tool | Authoritative arguments & evidence |
| --- | --- | --- | --- | --- |
| 1 | Check my last email. | gmail | `get_latest_email` | folder=all, max=1, query `-from:me`; `list_messages`+`get_message`; AUTH + MSG-ID |
| 2 | Check my last emails. | gmail | `get_latest_email` | same bounded lookup; AUTH + MSG-ID |
| 3 | Check my last 5 emails. | gmail | `get_latest_email` | max_results=5; detail reads bounded to 5 |
| 4 | Check my last 5 unread emails. | gmail | `search_emails` | query `is:unread -from:me`, max=5; not a generic latest lookup |
| 5 | Check my last 5 emails from John. | gmail | `get_latest_email` | sender=John, max=5, query `from:"John"` |
| 6 | Show my latest five emails. | gmail | `get_latest_email` | word-count "five" → max_results=5 |
| 7 | Show all emails received today. | gmail | `summarize_today` | max_results=50; local-day `after:`/`before:`/`-from:me` |
| 8 | Summarize important emails from today | gmail | `summarize_important` | period=today, max=10, importance source validated |
| 9 | Show unread emails | gmail | `summarize_unread` | query `is:unread`, max=5 |
| 10 | Show unread emails received today | gmail | `search_emails` | local-day range + `is:unread` |
| 11 | Show today's emails from Alice. | gmail | `search_emails` | day range + `from:"Alice"` |
| 12 | Show emails from today from Alice. | gmail | `search_emails` | day range + `from:"Alice"` (sender preserved) |
| 13 | Check important emails from today. | gmail | `summarize_important` | period=today |
| 14 | Summarize today's unread emails. | gmail | `search_emails` | day range + `is:unread` |
| 15 | Search emails from alice@example.com. | gmail | `search_emails` | sender=`alice@example.com` |
| 16 | Find emails with subject Project Update. | gmail | `search_emails` | subject=`Project Update` |
| 17 | Find emails from Alice. | gmail | `search_emails` | sender=`Alice` |
| 18 | Find emails from June 15 to June 20, 2025. | gmail | `search_emails` | `after:2025/06/15 before:2025/06/21` (year propagated) |
| 19 | Search emails from Gmail. | gmail | `search_emails` | sender=`Gmail` (not a provider claim) |
| 20 | Search emails from 2024. | gmail | `search_emails` | `after:2023/12/31 before:2025/01/01` |
| 21 | Show emails from November 10 to November 15, 2026. | gmail | `search_emails` | `after:2026/11/10 before:2026/11/16` |
| 22 | Open that email. | general | `general` | no Gmail read; no context to open |
| 23 | What did the sender ask me to do? | gmail | `get_latest_email` | read-only follow-up, not `gmail_reply` |
| 24 | Summarize the latest conversation with Alice. | gmail | `summarize_latest_thread` | query=Alice; `get_thread` scoped |
| 25 | Show the latest reply in the thread. | gmail | `get_thread` | context `thread_id=context-thread-1`; read-only wins over reply |
| 26 | Reply to that email. | gmail | `gmail_reply` | mutation plan only; **no tool executed** in audit |
| 27 | Check my last email. | gmail | `get_latest_email` | repeat for idempotence |
| 28 | Show all emails received today. | gmail | `summarize_today` | repeat |
| 29 | Show unread emails | gmail | `summarize_unread` | repeat |
| 30 | Summarize important emails from today | gmail | `summarize_important` | repeat |
| 31 | Search emails from alice@example.com. | gmail | `search_emails` | repeat |
| 32 | Find emails with subject Project Update. | gmail | `search_emails` | repeat |
| 33 | Show my latest five emails. | gmail | `get_latest_email` | repeat |
| 34 | Gmail API permission failure. | general | `general` | no Gmail read on failure wording |
| 35 | Incomplete tool batch. | general | `general` | no Gmail read |
| 36 | Clarification pending decision. | general | `general` | no Gmail read |
| 37 | Check my email and calendar for today. | gmail | `clarify` | mixed gmail/calendar; no read |
| 38 | Schedule a meeting for tomorrow. | google_calendar | `google_calendar` | `create_calendar_event_proposal` |
| 39 | Find an email from Alice and show Alex's phone number. | gmail | `clarify` | mixed google services; no read |
| 40 | Add Alex Johnson to my contacts. | google_contacts | `google_contacts` | no Gmail read |
| 41 | What's on my calendar today? | google_calendar | `google_calendar` | `get_today_schedule` |
| 42 | The last email was from Alice. What's her phone number? | google_contacts | `google_contacts` | contact domain boundary |

Full per-case assertions (including all query fragments, tool sets, identity, and evidence checks) live in the test file and were executed with `44 passed`.

---

## 9. Retrieval, counters, and bounded read ceiling

Audited and fixed:

- `_cap_result` previously kept stale `retrieved_count`, `retrieved_message_ids`, `failed_message_ids`, and `gmail_sources` after dropping messages. It now recomputes all counters, filters sources to displayed IDs, raises `remaining_count`, and sets `has_more`.
- `_collect_message_pages_bounded` reported `has_more=False` when it exited by exhausting `max_pages` with a live `next_page_token`. It now sets `has_more=True` whenever the estimate or token shows more work.
- Out-of-window messages from a day query were counted as retrieval failures. They are now excluded from the candidate set and recorded in `out_of_window_message_ids`, not `failed_message_ids`.
- `MAX_GMAIL_READ_RESULTS = 50` verified unchanged; every matrix read call asserts `max_results <= 50`.

Counters now use the exact required names: `requested_count`, `candidate_count`, `retrieved_count`, `processed_count`, `failed_count`, `remaining_count`, `content_truncated`, `has_more`.

---

## 10. Claim-level grounding audit

Every rendered summary entry is validated against its **own** authoritative message:

- `normalize_summary_message` copies only authoritative fields and propagates `body_truncated` and attachment metadata.
- `_compact_message` preserves bounded attachment metadata (filename, MIME type, ID, size, inline flag) so attachment claims can be verified.
- `extract_verified_batch_summaries` validates each entry individually against its message; a swapped summary is rejected (`verified == {}`), preventing cross-message fact leakage.
- The grounding validator rejects unsupported numeric and attachment claims; missing fields remain `NONE`/unavailable rather than being invented.
- Rejected summaries no longer render as `complete` or print "Emails summarized: 5".

Evidence codes from §7 are satisfied for all retrieved messages in the matrix.

---

## 11. Completeness status model

- Long Gmail completions are explicitly modelled as `complete`, `partial`, `failed`, `cancelled`, with `output_limited` and `finish_reason` retained.
- Short/direct Gmail answers now carry `response_metadata` built by `_gmail_retrieval_completeness`, so a bounded or truncated read is `partial`, never silently `complete`.
- Empty results with `remaining_count > 0` no longer claim "no incoming email"; they state that only an empty bounded result was verified and that no conclusion about other mail is possible.
- Short summaries with retrieval failures add a coverage note naming the failed/remaining counts.
- Old string **"This response may be incomplete because the AI reached its output limit. Ask me to continue from where it stopped."** was removed from the frontend (confirmed by grep; it remains only in a historical phase-31 investigation doc, which is not a user-facing surface).

---

## 12. Streaming verification

Backend streaming fixes, each covered by a regression in `test_chat_true_streaming.py` / the Phase 32 grounding file:

- `FallbackManager.stream` treats a provider that emits zero content chunks as `provider_empty_response` and falls back **only before the first token**; partial output is never switched mid-stream. True token streaming is preserved.
- A mid-stream client disconnect (`GeneratorExit`) with no persisted assistant message now marks the chat request terminal `failed`, so a same-idempotency-key retry is not permanently 409. The reconcile path is preserved when a message was already saved.
- `_stream_error_message` / `_stream_http_exception` no longer echo raw `ValueError` text into public SSE/HTTP responses; fixed safe messages are returned with unchanged status codes and conflict/idempotency messages.
- `ChatStreamEvent` now models the `completion` dict emitted in `done` events.

---

## 13. Persistence verification

- Streamed `done` completion metadata and persisted assistant content use the same status model, so a client that reconnects and refetches history sees the same completeness status that was streamed.
- The done-only replay path no longer renders an empty optimistic bubble before the refetch; the persisted/history bubble is used.
- Client-side Stop attaches `response_completion.status = 'cancelled'` to the optimistic bubble, matching the server-side cancellation model.

---

## 14. Frontend rendering verification

Fixes in `Assistant.jsx` and `conversationService.js`:

- Stop → resend no longer duplicates optimistic user/assistant messages; the idempotency key is reused while the superseded pair is dropped.
- `completion.status === 'unknown'` shows the exact notice `This response may be incomplete; its completion status could not be verified.`
- Post-token `status` events render as inline progress and persist until the next `status` or `done` event.
- Cancelled responses surface `This response was cancelled before email summaries were generated.`
- Malformed SSE frames reject with `Some streamed content could not be parsed.` and `partial: true`; later events including `done` are suppressed.
- Targeted frontend progression: 43 → 0 failures; full suite 388 passing.

---

## 15. Defects found, reproduced, and fixed

| # | Defect | Fix surface |
| --- | --- | --- |
| 1 | `_cap_result` stale counts and sources | `gmail_tools.py` |
| 2 | Pagination `has_more` false on page-cap exhaustion | `gmail_tools.py` |
| 3 | Out-of-window messages counted as failures | `gmail_tools.py` |
| 4 | Body truncation not flagged | `email_parser.py`, `gmail_tools.py`, `gmail_long_response.py` |
| 5 | Attachment metadata dropped | `gmail_tools.py`, `gmail_long_response.py` |
| 6 | Batch-wide validation allowed cross-message leakage | `gmail_long_response.py` |
| 7 | Numeric/attachment claims passed grounding | `gmail_agent_nodes.py` |
| 8 | Empty-with-remaining claimed no mail | `gmail_agent_nodes.py` |
| 9 | Retrieval failures not disclosed in short answers | `gmail_agent_nodes.py` |
| 10 | Rejected summaries still `complete` | `gmail_agent_nodes.py` |
| 11 | Generic empty search criteria | `gmail_agent_nodes.py` |
| 12 | Explicit "last N" counts ignored | `gmail_agent.py`, `gmail_agent_nodes.py`, `gmail_tools.py` |
| 13 | Subject / date-range / today modifiers lost | `gmail_agent_nodes.py` |
| 14 | Thread follow-up lost context scope | `gmail_agent_nodes.py` |
| 15 | Gmail claim guard too narrow | `chat_nodes.py` |
| 16 | Q04 unread count routed to latest lookup | `gmail_agent_nodes.py` |
| 17 | Q12 "from today from Alice" dropped sender | `gmail_agent_nodes.py` |
| 18 | Q18 date-range year not propagated | `gmail_agent_nodes.py` |
| 19 | Q25 read-only reply lost to `gmail_reply` | `gmail_agent_nodes.py` |
| 20 | Structured query operators matched as literal text | `gmail_tools.py` (`_GMAIL_STRUCTURED_QUERY_RE`) |
| 21 | Empty provider stream not falling back | `fallback.py` |
| 22 | Mid-stream disconnect not terminal | `chat_service.py` |
| 23 | Raw `ValueError` echoed to client | `chat.py` |
| 24 | `completion` not modelled in `ChatStreamEvent` | `chat_stream.py` |
| 25–31 | Frontend notices/progress/replay/duplicates/SSE | `Assistant.jsx`, `conversationService.js` |

All defects were reproduced with failing deterministic tests before the fix and retested to green afterward. No assertion was weakened to hide a failure; the only expectation corrections were to fixtures that were themselves wrong (oversized `_cap_result` fixture → 12 messages; swapped batch summaries → `verified == {}`; get-thread evidence shape → no `retrieved_count` field by design).

---

## 16. Reproduction workflow and commands

Workflow per defect: **Question → Execute → Inspect answer → Verify source evidence → Detect defect → Reproduce → Fix → Retest → Full regression.** Green tests alone were not treated as proof.

Exact commands and results (all from repository root unless noted):

| Command | Result |
| --- | --- |
| `..\.venv\Scripts\python.exe -m pytest -q` (baseline, backend) | 1441 passed, 12 warnings in 102.69s |
| `npm test -- --maxWorkers=2` (baseline, frontend) | 33 files, 385 tests passed |
| `..\.venv\Scripts\python.exe -m pytest -q tests/test_gmail_end_to_end_grounding_phase32.py` | 12 passed, 1 warning in 6.04s |
| Targeted backend suite (8 files) | 343 passed, 1 warning in 31.88s |
| `..\.venv\Scripts\python.exe -m pytest -q tests/test_gmail_end_to_end_matrix_phase32.py` | **44 passed in 2.82s** (baseline 6 failed / 38 passed before fixes) |
| Targeted backend suite (9 files incl. matrix) | 387 passed, 1 warning in 31.86s |
| `..\.venv\Scripts\python.exe -m pytest -q` (final, backend) | **1498 passed, 13 warnings in 78.93s** |
| `npm test -- --maxWorkers=2` (final, frontend) | **33 files, 388 tests passed in 49.64s** |
| `npm run lint` | 0 errors, warnings only |
| `npm run build` | ✓ 423 modules, built in 832ms |
| `(Get-FileHash -Algorithm SHA256 'backend/_debug_six_step.json').Hash` | `A10E99E319F5E2C28A94AAA4D56CB5C8A16D2B2D7B13FF713E56D5B666FFC1FF` (unchanged) |

Temporary probe files `backend/_phase32_probe.py` and `backend/_phase32_diag.py` were created for reproduction and **removed** before this report.

---

## 17. Constraints and working-tree integrity

- Protected fixture hash rechecked and unchanged (see §16).
- All changes remain unstaged/uncommitted/unpushed; `git status --short` shows only ` M` and `??` entries, no staged (`M ` / `A `) entries.
- Unrelated pre-existing modifications (calendar, contacts, Drive-adjacent, UI files, `package-lock.json`, etc.) were preserved; no unrelated file was reverted or overwritten.
- No `.env`, credential, API key, OAuth scope/consent, provider priority, approval workflow, isolation safeguard, or Drive status change.
- No staging, commit, push, deploy, or live Google/LLM call.
- New files added by this audit: `backend/tests/test_gmail_end_to_end_grounding_phase32.py`, `backend/tests/test_gmail_end_to_end_matrix_phase32.py`, `backend/app/workflows/gmail_long_response.py`, and this report.

---

## 18. Final verdict and remaining work

**FINAL VERDICT: NOT YET PRODUCTION-READY.**

Gate A passes with full automated coverage: 42-question matrix, message-level grounding, bounded counters, completeness/streaming/persistence, and frontend rendering — backend 1498 passing, frontend 388 passing, lint clean of errors, build successful.

Production readiness is withheld because:

1. **Gate B (local manual browser)** — NOT PERFORMED. Must be scripted and observed, not inferred from tests.
2. **Gate C (live authenticated Gmail)** — NOT PERFORMED. Requires explicit authorization and a read-only-only account; must verify real message IDs, senders, dates, and attachments.
3. **Gate D (production deployment)** — NOT PERFORMED. No deploy or production smoke test was run.

Until all three gates are completed and signed off, the correct status remains **NOT YET PRODUCTION-READY**. No deployment action should be taken based on this report alone.
