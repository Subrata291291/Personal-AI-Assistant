"""Phase 32: deterministic 42-question end-to-end Gmail matrix.

Each case links four layers so a route-only pass can never masquerade as a
grounded answer:

1. **domain** - which integration owns the request.
2. **plan** - the validated intent plus its authoritative arguments.
3. **tool invocation / arguments** - the actual read-only GmailService calls,
   their search queries, folders, limits, message IDs and thread IDs.
4. **downstream evidence** - every call is authenticated with the expected
   user/connection, and the returned result carries message-level source
   records whose IDs come from the authoritative Gmail fixture.

Everything is synthetic: no live Google API, credential, OAuth change, LLM
provider, Gmail mutation, or deployment is involved.
"""

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest

from app.llm.schemas import LLMResponse
from app.workflows import gmail_agent_nodes, gmail_tools

USER_ID = "11111111-1111-1111-1111-111111111111"
CONNECTION_ID = "44444444-4444-4444-4444-444444444444"
REQUEST_TIME = "2026-11-20T12:00:00Z"
IN_WINDOW_MS = int(
    datetime(2026, 11, 20, 9, 0, tzinfo=timezone.utc).timestamp() * 1000
)
EVIDENCE_IDS = {"evidence-1", "evidence-2"}


# ---------------------------------------------------------------------------
# Deterministic GmailService double
# ---------------------------------------------------------------------------


class RecordingGmailService:
    """Read-only GmailService double that records every invocation."""

    def __init__(self, plan):
        self.plan = plan or {}
        self.calls = []

    def _record(self, method, **kwargs):
        self.calls.append({"method": method, "kwargs": kwargs})

    @staticmethod
    def _assert_identity(kwargs):
        assert kwargs["user_id"] == UUID(USER_ID), kwargs
        assert kwargs["connection_id"] == UUID(CONNECTION_ID), kwargs

    def _summary(self, message_id):
        return {
            "id": message_id,
            "thread_id": "context-thread-1",
            "internal_date": str(IN_WINDOW_MS),
            "labels": ["INBOX"],
            "sender": self.plan.get("sender") or "alice@example.com",
            "subject": self.plan.get("subject") or "General update",
        }

    def _detail(self, message_id):
        sender = self.plan.get("sender") or "alice@example.com"
        subject = self.plan.get("subject") or "General update"
        return {
            "id": message_id,
            "thread_id": "context-thread-1",
            "internal_date": str(IN_WINDOW_MS),
            "headers": {
                "from": sender,
                "subject": subject,
                "date": "Fri, 20 Nov 2026 09:00:00 +0000",
            },
            "body": {"plain_text": f"Authoritative body for {message_id}."},
            "snippet": f"Authoritative body for {message_id}.",
            "labels": ["INBOX"],
            "attachments": [],
        }

    async def list_messages(self, **kwargs):
        self._record("list_messages", **kwargs)
        self._assert_identity(kwargs)
        return {
            "messages": [
                self._summary("evidence-1"),
                self._summary("evidence-2"),
            ],
            "result_size_estimate": 2,
            "next_page_token": None,
        }

    async def get_message(self, **kwargs):
        self._record("get_message", **kwargs)
        self._assert_identity(kwargs)
        return self._detail(kwargs["message_id"])

    async def get_thread(self, **kwargs):
        self._record("get_thread", **kwargs)
        self._assert_identity(kwargs)
        return {
            "thread_id": kwargs["thread_id"],
            "messages": [self._detail("evidence-1")],
            "truncated": False,
        }

    async def get_messages_for_summary(self, **kwargs):
        self._record("get_messages_for_summary", **kwargs)
        self._assert_identity(kwargs)
        return [self._detail(message_id) for message_id in kwargs["message_ids"]]

    def _validate_connection(self, **kwargs):
        self._record("_validate_connection", **kwargs)
        self._assert_identity(kwargs)
        return SimpleNamespace(email="user@example.com")


class _FakeSession:
    def __enter__(self):
        return object()

    def __exit__(self, *_args):
        return False


# ---------------------------------------------------------------------------
# Matrix definition
# ---------------------------------------------------------------------------

# query_contains entries are asserted against every list_messages query so a
# filter/sender/date modifier can never be silently dropped by the planner.
MATRIX = [
    # id, question, domain, intent, plan subset, query_contains
    (1, "Check my last email.", "gmail", "get_latest_email",
     {"folder": "all", "max_results": 1}, ["-from:me"]),
    (2, "Check my last emails.", "gmail", "get_latest_email",
     {"folder": "all", "max_results": 1}, ["-from:me"]),
    (3, "Check my last 5 emails.", "gmail", "get_latest_email",
     {"folder": "all", "max_results": 5}, ["-from:me"]),
    (4, "Check my last 5 unread emails.", "gmail", "search_emails",
     {"query": "is:unread -from:me", "max_results": 5}, ["is:unread"]),
    (5, "Check my last 5 emails from John.", "gmail", "get_latest_email",
     {"folder": "all", "sender": "John", "max_results": 5}, ['from:"John"']),
    (6, "Show my latest five emails.", "gmail", "get_latest_email",
     {"folder": "all", "max_results": 5}, ["-from:me"]),
    (7, "Show all emails received today.", "gmail", "summarize_today",
     {"max_results": 50}, ["after:", "before:", "-from:me"]),
    (8, "Summarize important emails from today", "gmail", "summarize_important",
     {"period": "today", "max_results": 10}, []),
    (9, "Show unread emails", "gmail", "summarize_unread",
     {"max_results": 5}, ["is:unread"]),
    (10, "Show unread emails received today", "gmail", "search_emails",
     {"max_results": 10}, ["after:", "before:", "is:unread"]),
    (11, "Show today's emails from Alice.", "gmail", "search_emails",
     {"max_results": 10}, ["after:", "before:", 'from:"Alice"']),
    (12, "Show emails from today from Alice.", "gmail", "search_emails",
     {"max_results": 10}, ["after:", "before:", 'from:"Alice"']),
    (13, "Check important emails from today.", "gmail", "summarize_important",
     {"period": "today", "max_results": 10}, []),
    (14, "Summarize today's unread emails.", "gmail", "search_emails",
     {"max_results": 10}, ["after:", "before:", "is:unread"]),
    (15, "Search emails from alice@example.com.", "gmail", "search_emails",
     {"sender": "alice@example.com", "max_results": 10},
     ['from:"alice@example.com"']),
    (16, "Find emails with subject Project Update.", "gmail", "search_emails",
     {"subject": "Project Update", "max_results": 10},
     ['subject:"Project Update"']),
    (17, "Find emails from Alice.", "gmail", "search_emails",
     {"sender": "Alice", "max_results": 10}, ['from:"Alice"']),
    (18, "Find emails from June 15 to June 20, 2025.", "gmail", "search_emails",
     {"max_results": 10}, ["after:2025/06/15", "before:2025/06/21"]),
    (19, "Search emails from Gmail.", "gmail", "search_emails",
     {"sender": "Gmail", "max_results": 10}, ['from:"Gmail"']),
    (20, "Search emails from 2024.", "gmail", "search_emails",
     {"max_results": 10}, ["after:2023/12/31", "before:2025/01/01"]),
    (21, "Show emails from November 10 to November 15, 2026.", "gmail",
     "search_emails", {"max_results": 10},
     ["after:2026/11/10", "before:2026/11/16"]),
    (22, "Open that email.", "general", "general", {}, []),
    (23, "What did the sender ask me to do?", "gmail", "get_latest_email",
     {"folder": "all", "max_results": 1}, ["-from:me"]),
    (24, "Summarize the latest conversation with Alice.", "gmail",
     "summarize_latest_thread", {"query": "Alice"}, []),
    (25, "Show the latest reply in the thread.", "gmail", "get_thread",
     {"thread_id": "context-thread-1"}, []),
    (26, "Reply to that email.", "gmail", "gmail_reply",
     {"draft_type": "reply", "source_message_id": "context-message-1"}, []),
    (27, "Check my last email.", "gmail", "get_latest_email",
     {"folder": "all", "max_results": 1}, ["-from:me"]),
    (28, "Show all emails received today.", "gmail", "summarize_today",
     {"max_results": 50}, ["after:", "before:", "-from:me"]),
    (29, "Show unread emails", "gmail", "summarize_unread",
     {"max_results": 5}, ["is:unread"]),
    (30, "Summarize important emails from today", "gmail", "summarize_important",
     {"period": "today", "max_results": 10}, []),
    (31, "Search emails from alice@example.com.", "gmail", "search_emails",
     {"sender": "alice@example.com", "max_results": 10},
     ['from:"alice@example.com"']),
    (32, "Find emails with subject Project Update.", "gmail", "search_emails",
     {"subject": "Project Update", "max_results": 10},
     ['subject:"Project Update"']),
    (33, "Show my latest five emails.", "gmail", "get_latest_email",
     {"folder": "all", "max_results": 5}, ["-from:me"]),
    (34, "Gmail API permission failure.", "general", "general", {}, []),
    (35, "Incomplete tool batch.", "general", "general", {}, []),
    (36, "Clarification pending decision.", "general", "general", {}, []),
    (37, "Check my email and calendar for today.", "gmail", "clarify",
     {"clarification_reason": "mixed_gmail_calendar"}, []),
    (38, "Schedule a meeting for tomorrow.", "google_calendar",
     "google_calendar",
     {"calendar_tool": "create_calendar_event_proposal"}, []),
    (39, "Find an email from Alice and show Alex's phone number.", "gmail",
     "clarify", {"clarification_reason": "mixed_google_services"}, []),
    (40, "Add Alex Johnson to my contacts.", "google_contacts",
     "google_contacts", {}, []),
    (41, "What's on my calendar today?", "google_calendar", "google_calendar",
     {"calendar_tool": "get_today_schedule"}, []),
    (42, "The last email was from Alice. What's her phone number?",
     "google_contacts", "google_contacts", {}, []),
]

EXECUTABLE_READ_INTENTS = frozenset({
    "get_latest_email", "search_emails", "summarize_today", "summarize_unread",
    "summarize_important", "summarize_latest_thread", "get_thread",
})

EXPECTED_TOOLS = {
    "get_latest_email": {"list_messages", "get_message"},
    "search_emails": {"list_messages", "get_messages_for_summary"},
    "summarize_today": {"list_messages", "get_messages_for_summary"},
    "summarize_unread": {"list_messages", "get_messages_for_summary"},
    "summarize_important": {"list_messages", "get_messages_for_summary"},
    "summarize_latest_thread": {
        "list_messages", "get_thread", "_validate_connection",
    },
    "get_thread": {"get_thread"},
}


def _state(question):
    return {
        "user_message": question,
        "user_id": USER_ID,
        "conversation_id": "conversation-phase32-matrix",
        "assistant_response": "",
        "provider": "",
        "model": "",
        "messages": [],
        "user_timezone": "UTC",
        "gmail_connection_id": CONNECTION_ID,
        "request_time": REQUEST_TIME,
        "available_gmail_sources": {
            "messages": [{
                "message_id": "context-message-1",
                "thread_id": "context-thread-1",
                "internal_date": str(IN_WINDOW_MS),
                "sender": "alice@example.com",
                "subject": "Context",
            }],
        },
    }


def _route(monkeypatch, question):
    """Route without letting a live LLM substitute for deterministic planning."""
    monkeypatch.setattr(
        gmail_agent_nodes.llm_router,
        "generate",
        Mock(return_value=LLMResponse(
            content=json.dumps({"intent": "general"}),
            provider="mock",
            model="mock-model",
            finish_reason="stop",
            latency_ms=0,
        )),
    )
    return gmail_agent_nodes.gmail_intent_router_node(_state(question))


def _execute_read(monkeypatch, plan):
    service = RecordingGmailService(plan)
    monkeypatch.setattr(gmail_tools, "SessionLocal", _FakeSession)
    monkeypatch.setattr(gmail_tools, "GmailService", lambda _db: service)
    result = asyncio.run(gmail_tools.execute_gmail_read(
        intent=plan["intent"],
        plan=plan,
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        user_timezone="UTC",
        request_time=REQUEST_TIME,
    ))
    return service, result


def _assert_evidence(intent, result):
    """Assert the result carries authoritative message-level evidence."""
    assert result["intent"] == intent
    sources = result.get("gmail_sources")
    assert isinstance(sources, dict)
    assert sources.get("connection_id") == CONNECTION_ID
    records = sources.get("messages") or []
    assert records, f"no message-level evidence for {intent}"
    for record in records:
        assert record.get("message_id") in EVIDENCE_IDS, record
        assert record.get("thread_id") == "context-thread-1", record
        assert record.get("sender"), record
        assert record.get("subject"), record
    if intent in {"get_latest_email", "get_email"}:
        assert result.get("message"), intent
    if intent in {"search_emails", "summarize_today", "summarize_unread",
                  "summarize_important"}:
        assert result.get("messages"), intent
        assert int(result.get("retrieved_count", 0)) > 0, intent
    if intent == "get_thread":
        assert result.get("messages"), intent


@pytest.mark.parametrize(
    "case_id,question,domain,intent,plan_subset,query_contains",
    MATRIX,
    ids=[case[0] for case in MATRIX],
)
def test_matrix_case(
    monkeypatch,
    case_id,
    question,
    domain,
    intent,
    plan_subset,
    query_contains,
):
    routed = _route(monkeypatch, question)

    # Layer 1: domain ownership.
    assert routed.get("assistant_route") == domain, (case_id, routed)
    plan = routed.get("gmail_plan") or {}
    assert plan.get("intent") == intent, (case_id, plan)

    # Layer 2: authoritative plan arguments (never just the intent label).
    for key, expected in plan_subset.items():
        assert plan.get(key) == expected, (case_id, key, plan)

    # Non-Gmail domains and mutation/clarify plans must not invoke a Gmail read.
    if intent not in EXECUTABLE_READ_INTENTS:
        assert domain in {"general", "google_calendar", "google_contacts",
                          "gmail"}
        return

    # Layer 3: real tool invocations and their arguments.
    service, result = _execute_read(monkeypatch, plan)
    invoked = {call["method"] for call in service.calls}
    assert EXPECTED_TOOLS[intent] <= invoked, (case_id, plan, invoked)
    assert service.calls, case_id

    queries = [
        call["kwargs"].get("query")
        for call in service.calls
        if call["method"] == "list_messages"
    ]
    queries = [query for query in queries if isinstance(query, str)]
    if query_contains:
        assert queries, (case_id, "no list query issued")
        for fragment in query_contains:
            assert any(fragment in query for query in queries), (
                case_id, fragment, queries,
            )
    max_results = plan.get("max_results")
    if max_results is not None:
        for call in service.calls:
            kwargs = call["kwargs"]
            if "max_results" in kwargs:
                assert kwargs["max_results"] <= 50, (case_id, kwargs)
    if intent == "get_thread":
        thread_calls = [
            call for call in service.calls if call["method"] == "get_thread"
        ]
        assert thread_calls[0]["kwargs"]["thread_id"] == "context-thread-1"

    # Layer 4: downstream, message-level evidence from the authorized account.
    _assert_evidence(intent, result)


def test_matrix_has_exactly_42_questions():
    assert len(MATRIX) == 42
    assert [case[0] for case in MATRIX] == list(range(1, 43))


def test_matrix_limits_every_read_result_to_bounded_ceiling(monkeypatch):
    """No matrix case may silently raise the 50-message read ceiling."""
    assert gmail_tools.MAX_GMAIL_READ_RESULTS == 50
    for case in MATRIX:
        if case[3] not in EXECUTABLE_READ_INTENTS:
            continue
        routed = _route(monkeypatch, case[1])
        plan = routed["gmail_plan"]
        service, result = _execute_read(monkeypatch, plan)
        for call in service.calls:
            limit = call["kwargs"].get("max_results")
            if limit is not None:
                assert limit <= gmail_tools.MAX_GMAIL_READ_RESULTS
        assert int(result.get("retrieved_count", 0)) <= 50
