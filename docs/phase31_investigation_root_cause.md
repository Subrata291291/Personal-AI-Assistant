# Phase 31 — Investigation and Root-Cause Evidence

## Scope

This document records the code-level investigation performed before any Phase 31 production change. It covers the complete response-generation path for a request such as:

> Check my all today's mail and summarize them step by step.

Only code tracing and deterministic evidence were used. No live Google API, live LLM provider, mutation, deployment, credential, `.env`, OAuth scope, or unrelated working-tree change was involved.

## Verdict

The user-visible truncation is caused by a **combination of retrieval, normalization, aggregation, and output-limit handling defects**:

1. The request is silently capped at 10 messages; "all today's mail" is not represented as a bounded-but-disclosed retrieval.
2. `summarize_today` builds summaries from list metadata without fetching authoritative message details by ID. Those metadata records lose sender, subject, date, and body in `_compact_message`, leaving the final LLM with IDs, snippets, and labels but not the authoritative per-email fields.
3. One final LLM request receives all compacted message JSON and is asked to produce every email card. Its hardcoded `max_tokens=1800` (bounded further by provider caps) is not enough for a long per-email report.
4. That final call has no output-limit recovery. The `length`/`max_tokens` finish reason is merely persisted as `response_completion.status = "truncated"`, which produces the exact frontend warning:
   > This response may be incomplete because the AI reached its output limit. Ask me to continue from where it stopped.

It is **not** primarily proven to be an SSE, frontend rendering, persistence, tenant-isolation, provider-priority, or LangGraph node-output truncation defect. Those layers preserve the content they are given, but they currently receive already-truncated or incomplete content.

---

## 1. Intent routing and planning

Deterministic routing runs before the LLM planner:

- `backend/app/workflows/gmail_agent_nodes.py:5662-5688` — `_deterministic_assistant_plan` is attempted first.
- `backend/app/workflows/gmail_agent_nodes.py:3443-3450` — any mail/message/inbox wording combined with "today" routes directly to `SummarizeTodayPlan(intent="summarize_today")`.
- `backend/app/workflows/gmail_agent_nodes.py:4255-4261` — unread wording routes to `SummarizeUnreadPlan`.
- `backend/app/workflows/gmail_agent_nodes.py:4200-4211` and `:4080-4109` — important-mail wording builds `SummarizeImportantPlan`; explicit "top/first/last N emails" is only honored for N ≤ 10 at `:4101-4108`.
- `backend/app/workflows/gmail_agent_nodes.py:3390-3420`, `:3467-3473`, `:4112+` — latest-N and date-range wording resolve to `SearchEmailsPlan`.
- `backend/app/workflows/gmail_agent_nodes.py:412-415` — the planner prompt itself tells the model search limits are 1 through 10.

The LLM planner is at `gmail_agent_nodes.py:5722-5755`; it uses `max_tokens=300`. Its output-limit branch at `:5774-5792` falls back to a safe plan, but this branch applies only to planning, not the final answer.

### Proven defect: "all" is capped without disclosure

`backend/app/schemas/gmail_agent.py:253-255` defines `SummarizeTodayPlan.max_results` with default 10 and `le=10`. Similar caps exist at:

- `gmail_agent.py:151-159` — `SearchEmailsPlan.max_results`, default 5, max 10.
- `gmail_agent.py:194-196` — `SummarizeImportantPlan.max_results`, default 10, max 10.
- `gmail_agent.py:243-250` — urgent/attention plans, default 10, max 10.
- `gmail_agent.py:302-304` — `SummarizeUnreadPlan.max_results`, default 5, max 5.

`backend/app/workflows/gmail_tools.py:1176` slices today's validated messages with `[: validated.max_results]`, and `:1203` sets `result_size_estimate = len(summaries)` **after** the cap. Therefore, a user asking for all of today's mail receives at most 10, and the result metadata reports 10 as if that were the full result. There is no `content_truncated`, `remaining`, or `requested` signal.

---

## 2. Gmail search, pagination, and detail retrieval

Pagination is implemented in `backend/app/workflows/gmail_tools.py:354-401`:

- `next_page_token` is followed at `:397-400`.
- Duplicate IDs are skipped at `:366` and `:383-388`.
- It is bounded by `MAX_LIST_PAGES = 5` at `:34`.
- `GmailService._list_messages` validates `1 <= max_results <= 50` at `backend/app/services/gmail_service.py:389-393`; `MAX_LIST_RESULTS = 50` at `:121`.
- List results are metadata-only (`format=metadata`) at `gmail_service.py:443-459`; they contain no body and no `headers` map. The flattened top-level fields are produced at `gmail_service.py:461-474`.

Full message details come from `GmailService.get_message` at `gmail_service.py:505-529` or `get_messages_for_summary` at `:531-562`, both ultimately calling `_get_message` at `:564-593` with `format=full`.

Current usage:

- `summarize_unread` — `gmail_tools.py:1010-1038`, one list call, then `_fetch_details`.
- `summarize_important` — `gmail_tools.py:1040-1096`, pagination then `_fetch_details`.
- `summarize_urgent` / `summarize_attention` — `gmail_tools.py:1098-1139`, one list call then `_fetch_details`.
- `search_emails` — `gmail_tools.py:762-842`, multiple queries, ID deduplication at `:779`, and per-candidate `get_message` verification at `:796-802`.
- `summarize_today` — `gmail_tools.py:1141-1204`, local-day epoch validation at `:1168-1175`, then compact list metadata only at `:1195-1198`.

`_fetch_details` at `gmail_tools.py:1285-1303` takes only `summaries[:10]`, and `GmailService.get_messages_for_summary` rejects more than 10 IDs at `gmail_service.py:537-538`. There is no bounded multi-chunk loop, so 20 or 30 requested messages cannot all receive authoritative detail reads.

`asyncio.gather` is used without `return_exceptions=True` for detail reads at `gmail_service.py:557-562`; one failed message aborts the whole chunk. No per-message failed-ID list is returned to the response node.

### Proven defect: bounded retrieval and failure tracking do not exist

A 30-email request cannot currently:

- retrieve details for all 30 because of the `[:10]` slice and the service-level 10-ID cap;
- continue after one failed detail read;
- report retrieved, processed, failed, and remaining counts;
- distinguish "all requested items were processed" from "the bounded request was satisfied but more results remain in Gmail".

---

## 3. Authoritative normalization and field loss

`GmailEmailParser.parse_message` at `backend/app/integrations/google/email_parser.py:223-249` returns `id`, `thread_id`, `snippet`, `internal_date`, `headers`, `body`, `labels`, and attachments. Header and body extraction are at `:159-186` and `:76-157`.

`backend/app/workflows/gmail_tools.py:514-541` defines `_compact_message`. It reads only:

- `headers.from`
- `headers.to`
- `headers.cc`
- `headers.subject`
- `headers.date`
- `body.plain_text` or converted HTML

However, Gmail list metadata has top-level `sender`, `recipient`, `subject`, and `date`, with no `headers` and no `body` (`gmail_service.py:461-474`). By contrast, `_source_record` at `gmail_tools.py:544-582` already has top-level fallbacks, but it is used only for `gmail_sources` at `:585-598`, which are later excluded from the LLM input.

### Proven defect: today's summaries lose authoritative fields

For `summarize_today`, `_compact_message(item)` at `gmail_tools.py:1195-1198` receives metadata-only list items. The resulting message object has empty `sender`, `subject`, `date`, and `body`; only `message_id`, `internal_date`, `snippet`, and labels survive. The final LLM is then asked to render sender/subject/date/action/deadline cards from depleted data.

This is directly evidenced by `backend/tests/test_phase14_gmail_intelligence_regression.py:265-266`, which currently asserts that boundary filtering must not fetch details.

---

## 4. Per-email summarization and batch aggregation

There is no per-email or per-batch summarization path today. The only read-summary LLM call is the single final call in `backend/app/workflows/gmail_agent_nodes.py:7433-7525`:

- system prompt: `:7438-7493`;
- card template requiring one block per email: `:7454-7485`;
- all compacted messages serialized into one user message: `:7504-7507`;
- `temperature=0` for summary intents: `:7519-7523`;
- `max_tokens=1800`: `:7524`.

`data` deliberately excludes `gmail_sources` at `:7414-7418`, so message IDs are never supplied to the summarizing LLM. There is therefore no way for an intermediate summary to retain the original source reference.

Grounding validation happens only after the whole answer is generated:

- `_gmail_summary_is_grounded` at `gmail_agent_nodes.py:7292-7331`;
- if invalid, `UNVERIFIED_GMAIL_SUMMARY_RESPONSE` replaces the answer at `:7533-7538`.

The count is not computed by the backend. The prompt merely instructs the LLM to write `<actual count>` at `:7464` and "State the actual result count" at `:7484`. The user-visible "10 emails analyzed" is therefore an LLM-authored count over a backend-capped collection, not an authoritative completeness claim.

### Proven defect: one oversized LLM call

All raw compacted messages are sent to one request with a fixed 1800-token output budget. For 10 messages, each with a multi-field card, the output budget is easily exhausted. The provider returns `finish_reason="length"`, and the partially generated Markdown is persisted as if it were the answer.

---

## 5. Provider output limits and fallback boundary

Detection:

- `backend/app/llm/schemas.py:54-63` — `LLMResponse.output_limited` recognizes `length`, `max_tokens`, `max token`, names containing `max_tokens`/`max tokens`, and reasons ending `.length`.
- `backend/app/llm/schemas.py:65-85` — `completion_metadata()` maps output limits to `status="truncated"`.
- `backend/app/llm/schemas.py:99-105` — `provider_output_token_limit()` returns `min(requested_limit, provider_limit)`.
- `backend/app/core/config.py:50,56,62,68` — provider output caps default to 2048.
- Effective limits: Gmail final summary 1800, drafts/extractions 1200, planner 300, general chat 1800.

Streaming and fallback:

- `backend/app/llm/streaming.py:120-125` — provider chunks are forwarded immediately as token events.
- `backend/app/llm/streaming.py:135-144` — final `finish_reason` is copied into `LLMResponse`.
- `backend/app/llm/router.py:30,59` — generate and stream delegate to `FallbackManager`.
- `backend/app/llm/fallback.py:73` and `backend/app/core/config.py:127-134` — priority is Groq → OpenRouter → Gemini → OpenAI.
- `backend/app/llm/fallback.py:737-756` — `content_emitted` changes on the first non-empty token.
- `backend/app/llm/fallback.py:792-796,826-828` — fallback is forbidden after the first content token; a second provider cannot append to partial output.
- `backend/tests/test_chat_true_streaming.py:753-794` — existing tests prove fallback-before-first-token and no-append-after-first-token.

`output_limited` causes no continuation in the final Gmail node. Router only logs it at `router.py:35-47`. The planner has its own fallback at `gmail_agent_nodes.py:5774-5792`, the draft path raises at `:6589-6592`, meeting extraction returns a cautious answer at `:6949-6960`, but the final summary path at `:7433-7543` never branches on it.

### Proven defect: warning without recovery

The provider correctly reports the output-limit condition. The application stores and displays it but does not automatically complete the work. Merely increasing `max_tokens` would not address the 10-message cap, missing details, missing counts, or grounding/source-reference gaps.

---

## 6. ChatService, SSE, and persistence

- `backend/app/services/chat_service.py:1091-1119` — sanitizes custom stream events.
- `chat_service.py:1933-1950` — status events are deduplicated; token events append and replace events overwrite `streamed_text`.
- `chat_service.py:2638-2646` — final parity guarantees the rendered content equals the persisted content by emitting one final token or `replace`.
- `chat_service.py:2073-2096` — one assistant message is persisted with provider/model metadata and `response_completion`.
- `backend/app/services/message_service.py:84-97` — unique `(chat_request_id, role)` enforces one assistant message per request.
- `backend/app/api/v1/endpoints/chat.py:365-380` — `done` payload carries IDs/provider/model but no body or metadata.
- `frontend/src/pages/Assistant.jsx:540-568` — appends token events and overwrites on replace.
- `Assistant.jsx:875-886` — renders the exact output-limit warning when `metadata.response_completion.status === "truncated"`.
- `frontend/src/services/conversationService.js:227-231` — a stream that ends without `done` is treated as partial/incomplete.

The streaming layer is genuine: provider deltas are forwarded immediately and existing tests prove the first token arrives before generation completes. Persistence likewise retains the full string it receives. These layers are not the source of the missing email summaries.

One related weakness: progress status is rendered only inside the typing indicator, which is hidden once content starts (`Assistant.jsx:544,558,870-874`). Mid-batch progress would therefore be invisible unless the frontend is adjusted.

---

## 7. Root-cause classification against the requested candidates

| Candidate | Verdict | Evidence |
|---|---|---|
| Provider max-output-token limits | **Contributing / direct trigger** | `gmail_agent_nodes.py:7524`; `llm/schemas.py:54-85,99-105`; `config.py:50-68`. |
| Excessive prompt/context size | **Contributing** | All compacted messages JSON in one user message at `gmail_agent_nodes.py:7504-7507`; 24k result cap at `gmail_tools.py:32,601-627`. |
| Summarizing too many emails in one LLM call | **Primary** | No per-email/per-batch path; full card template for every email at `gmail_agent_nodes.py:7468-7476,7433-7525`. |
| Missing pagination or incomplete retrieval | **Primary** | Silent 10 cap at `gmail_tools.py:1176,1203`; `_fetch_details` `[:10]` at `:1291-1297`; service 10-ID cap at `gmail_service.py:537-538`. |
| LangGraph node output truncation | Not proven | Node returns the full `answer` string at `gmail_agent_nodes.py:7555-7576`. |
| ChatService or SSE truncation | Not proven | Final parity replace at `chat_service.py:2638-2646`; SSE event cap only applies to a single oversized event at `:68,1107`. |
| Frontend rendering or persistence | Not proven as source | Verbatim append/replace at `Assistant.jsx:548,562`; full persistence at `chat_service.py:2073-2324`. Status visibility is a separate UX gap. |
| Combination of issues | **Confirmed** | Retrieval cap + metadata field loss + single oversized LLM call + no output-limit recovery. |

---

## 8. Required fix direction derived from evidence

The smallest safe solution must:

1. Raise bounded read limits so 20/30-email requests are representable, while disclosing any remaining results instead of claiming "all".
2. Follow Gmail pagination and fetch authoritative details in chunks of at most 10 message IDs.
3. Preserve `message_id`, `thread_id`, sender, subject, date, snippet, and body through normalization.
4. Deduplicate by message ID and track requested, retrieved, processed, failed, and remaining counts.
5. Summarize small batches independently with bounded prompts, using deterministic fallbacks when a batch is output-limited or ungrounded.
6. Aggregate verified intermediate summaries deterministically in bounded sections, not by sending all raw bodies to one final LLM call.
7. Emit explicit `complete` / `partial` / `failed` / `cancelled` completeness metadata; never infer completeness from streamed text length.
8. Keep provider priority and all existing token-streaming safety rules; never append another provider after the first token.
9. Use the existing `replace` path whenever validated final content differs from streamed intermediate content.
10. Preserve Phase 30 grounding behavior and all unrelated working-tree changes.

No code change was made while producing this investigation document.
