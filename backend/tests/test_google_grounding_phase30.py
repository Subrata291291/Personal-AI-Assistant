"""Phase 30: authoritative grounding regressions for Google integrations.

Every test uses deterministic mocks and synthetic fixtures. No live Google API
call, approval, or write is performed.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from app.llm.schemas import LLMResponse
from app.schemas.chat_stream import ChatStreamEvent
from app.services.chat_service import ChatService
from app.workflows import chat_nodes, contacts_read, gmail_agent_nodes, gmail_tools


def _llm_response(content: str) -> LLMResponse:
    return LLMResponse(
        content=content,
        provider="test",
        model="test",
        finish_reason="stop",
        latency_ms=0,
        output_limited=False,
    )


def _gmail_state(message: str, **overrides):
    state = {
        "user_message": message,
        "user_id": "user-phase30",
        "conversation_id": "conversation-phase30",
        "assistant_response": "",
        "provider": "",
        "model": "",
        "messages": [],
        "user_timezone": "Asia/Kolkata",
        "gmail_connection_id": "conn-gmail-phase30",
    }
    state.update(overrides)
    return state


# ---------------------------------------------------------------------------
# Final-response grounding guards
# ---------------------------------------------------------------------------


def test_daily_planning_llm_cannot_assert_unverified_calendar_fact(monkeypatch):
    """Unverified Calendar claims must be blocked on every LLM-backed route."""
    fabricated = "You have a meeting with Acme at 3 PM on your calendar."
    monkeypatch.setattr(
        chat_nodes,
        "generate_or_stream",
        Mock(return_value=_llm_response(fabricated)),
    )

    result = chat_nodes.llm_node(
        {
            "user_message": "Plan my day",
            "messages": [],
            "user_timezone": "Asia/Kolkata",
            "assistant_route": "daily_planning",
        }
    )

    assert result["assistant_response"] == chat_nodes.UNVERIFIED_CALENDAR_RESPONSE
    assert fabricated not in result["assistant_response"]


@pytest.mark.parametrize(
    ("route", "fabricated"),
    [
        ("general", "Your latest emails are from secret@example.com."),
        ("daily_planning", "Alex's phone number is +1 555 000 1234."),
    ],
)
def test_unverified_gmail_or_contacts_claim_is_blocked(monkeypatch, route, fabricated):
    monkeypatch.setattr(
        chat_nodes,
        "generate_or_stream",
        Mock(return_value=_llm_response(fabricated)),
    )

    result = chat_nodes.llm_node(
        {
            "user_message": "Tell me about my Google data",
            "messages": [],
            "user_timezone": "Asia/Kolkata",
            "assistant_route": route,
        }
    )

    assert result["assistant_response"] == chat_nodes.UNVERIFIED_GOOGLE_DATA_RESPONSE
    assert fabricated not in result["assistant_response"]


def test_grounding_guard_emits_replace_for_streaming_parity(monkeypatch):
    fabricated = "Your schedule shows a 3pm sync with Acme."
    replace = Mock()
    monkeypatch.setattr(chat_nodes, "generate_or_stream", Mock(return_value=_llm_response(fabricated)))
    monkeypatch.setattr(chat_nodes, "emit_replace", replace)

    result = chat_nodes.llm_node(
        {
            "user_message": "Plan my day",
            "messages": [],
            "user_timezone": "Asia/Kolkata",
            "assistant_route": "daily_planning",
        }
    )

    replace.assert_called_once_with(chat_nodes.UNVERIFIED_CALENDAR_RESPONSE)
    assert result["assistant_response"] == chat_nodes.UNVERIFIED_CALENDAR_RESPONSE


def test_gmail_summary_validator_rejects_unsupported_personal_claims():
    evidence = {
        "messages": [
            {
                "message_id": "gmail-message-1",
                "thread_id": "gmail-thread-1",
                "sender": "alice@example.com",
                "subject": "Project Update",
                "date": "November 15, 2026",
                "body": "The report is ready for review.",
            }
        ],
        "period": "today",
        "search_criteria": "is:unread",
    }

    assert gmail_agent_nodes._gmail_summary_is_grounded(
        "**From:** alice@example.com\n**Subject:** Project Update",
        evidence,
    )
    assert not gmail_agent_nodes._gmail_summary_is_grounded(
        "**From:** mallory@evil.example\n**Subject:** Project Update",
        evidence,
    )
    assert not gmail_agent_nodes._gmail_summary_is_grounded(
        "**Deadline:** December 25, 2099",
        evidence,
    )


def test_gmail_response_node_replaces_unverified_summary(monkeypatch):
    result = {
        "messages": [
            {
                "message_id": "gmail-message-1",
                "sender": "alice@example.com",
                "subject": "Project Update",
                "date": "November 15, 2026",
                "body": "The report is ready for review.",
            }
        ],
        "period": "today",
        "search_criteria": "is:unread",
    }
    fabricated = "**From:** mallory@evil.example\n**Subject:** Project Update"
    monkeypatch.setattr(
        gmail_agent_nodes,
        "generate_or_stream",
        Mock(return_value=_llm_response(fabricated)),
    )
    replace = Mock()
    monkeypatch.setattr(gmail_agent_nodes, "emit_replace", replace)

    response = gmail_agent_nodes.gmail_response_node(
        {
            "user_message": "Show unread emails",
            "gmail_plan": {"intent": "summarize_unread"},
            "provider": "",
            "model": "",
            "messages": [],
        },
        result=result,
    )

    assert response["assistant_response"] == gmail_agent_nodes.UNVERIFIED_GMAIL_SUMMARY_RESPONSE
    assert response["response_metadata"]["gmail_grounding_verified"] is False
    replace.assert_called_once_with(gmail_agent_nodes.UNVERIFIED_GMAIL_SUMMARY_RESPONSE)


def test_gmail_summarization_prompt_warns_on_truncated_data(monkeypatch):
    captured = {}

    def fake_generate(request):
        captured["request"] = request
        return _llm_response("I found a limited set of Gmail results.")

    monkeypatch.setattr(gmail_agent_nodes, "generate_or_stream", fake_generate)
    monkeypatch.setattr(gmail_agent_nodes, "emit_replace", Mock())

    gmail_agent_nodes.gmail_response_node(
        {
            "user_message": "Show unread emails",
            "gmail_plan": {"intent": "summarize_unread"},
            "provider": "",
            "model": "",
            "messages": [],
        },
        result={
            "messages": [
                {
                    "message_id": "gmail-message-1",
                    "sender": "alice@example.com",
                    "subject": "Project Update",
                }
            ],
            "content_truncated": True,
            "search_criteria": "is:unread",
        },
    )

    prompt = captured["request"].messages[-1].content
    assert "supplied data was truncated" in prompt
    assert "do not claim that no other messages" in prompt


def test_gmail_thread_dates_use_user_timezone_with_legacy_alias():
    # 2026-11-15 23:30 UTC is already 2026-11-16 in Asia/Kolkata.
    epoch_ms = int(datetime(2026, 11, 15, 23, 30, tzinfo=timezone.utc).timestamp() * 1000)
    message = {
        "id": "gmail-message-1",
        "internal_date": epoch_ms,
        "headers": {
            "from": "Alice <alice@example.com>",
            "to": "me@example.com",
            "subject": "Project Update",
            "date": "Sun, 15 Nov 2026 23:30:00 +0000",
        },
        "body": {"plain_text": "Please review."},
    }

    timeline, summary = gmail_tools._thread_timeline(
        [message],
        "me@example.com",
        "Asia/Kolkata",
    )

    assert summary["presentation_timezone"] == "Asia/Kolkata"
    assert summary["latest_message_date_local"].startswith("2026-11-16T05:00")
    assert summary["latest_message_date_asia_kolkata"] == summary["latest_message_date_local"]
    assert timeline[0]["date_local"] == summary["latest_message_date_local"]


# ---------------------------------------------------------------------------
# Routing / question matrix (authoritative domain, no free-form fallback)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("question", "route", "tool"),
    [
        ("Check my last email.", "gmail", "get_latest_email"),
        ("Show my latest five emails.", "gmail", "get_latest_email"),
        ("Show all emails received today.", "gmail", "summarize_today"),
        ("Show unread emails.", "gmail", "summarize_unread"),
        ("Search emails from alice@example.com.", "gmail", "search_emails"),
        ("Find emails with subject Project Update.", "gmail", "search_emails"),
        ("Summarize the email from Alice.", "gmail", "get_latest_email"),
        ("What did the sender ask me to do?", "gmail", "get_latest_email"),
        ("Show the latest reply in the thread.", "gmail", "summarize_latest_thread"),
        ("What is the date of the latest email?", "gmail", "get_latest_email"),
        ("Do I have an email from Alice Smith?", "gmail", "get_latest_email"),
        ("Show emails from November 10 to November 15, 2026.", "gmail", "search_emails"),
        ("What is my next event?", "google_calendar", "list_calendar_events"),
        ("Show today's events.", "google_calendar", "get_today_schedule"),
        ("Show tomorrow's schedule.", "google_calendar", "get_calendar_schedule"),
        ("Am I free tomorrow?", "google_calendar", "check_calendar_availability"),
        ("Am I free between 11 AM and 2 PM?", "google_calendar", "check_calendar_availability"),
        ("Why am I busy during that interval?", "google_calendar", "list_calendar_events"),
        ("Show all events on November 15, 2026.", "google_calendar", "get_calendar_schedule"),
        ("Find an event titled Gym session.", "google_calendar", "list_calendar_events"),
        ("Find an event with Lunch in the title.", "google_calendar", "list_calendar_events"),
        ("Find the Gym time for next Monday.", "google_calendar", "list_calendar_events"),
        ("What time does the Project Sync event start?", "google_calendar", None),
        ("What is the event's timezone?", "google_calendar", None),
        ("Show recurring events.", "google_calendar", None),
        ("Show my contacts.", "google_contacts", None),
        ("Search for a contact named Alex.", "google_contacts", None),
        ("Find a contact by email alice@example.com.", "google_contacts", None),
        ("Show Alex's phone number.", "google_contacts", None),
        ("Show Alex's organization.", "google_contacts", None),
        ("Show Alex's job title.", "google_contacts", None),
        ("Show Alex's address.", "google_contacts", None),
        ("Find contacts with similar names.", "google_contacts", None),
    ],
)
def test_question_matrix_uses_authoritative_domain(monkeypatch, question, route, tool):
    # A deterministic route must not depend on a live provider call.
    monkeypatch.setattr(
        gmail_agent_nodes.llm_router,
        "generate",
        Mock(return_value=_llm_response(json.dumps({"intent": "general"}))),
    )

    result = gmail_agent_nodes.gmail_intent_router_node(_gmail_state(question))
    plan = result.get("gmail_plan") or {}

    assert result["assistant_route"] == route
    assert plan.get("intent") == route or (
        route == "gmail" and plan.get("intent") != "general"
    )
    if tool is not None:
        assert plan.get("calendar_tool") == tool or plan.get("intent") == tool


def test_event_time_and_why_busy_plans_are_bounded_calendar_reads():
    busy_plan, route = gmail_agent_nodes._deterministic_calendar_plan(
        _gmail_state("Why am I busy during that interval?")
    )
    assert route == "google_calendar"
    assert busy_plan["calendar_tool"] == "list_calendar_events"
    assert busy_plan["calendar_arguments"]["calendar_id"] == "primary"
    assert busy_plan["calendar_arguments"]["time_zone"] == "Asia/Kolkata"

    gym_plan, route = gmail_agent_nodes._deterministic_calendar_plan(
        _gmail_state("Find the Gym time for next Monday.")
    )
    assert route == "google_calendar"
    assert gym_plan["calendar_tool"] == "list_calendar_events"
    assert gym_plan["calendar_arguments"]["query"].casefold() == "gym"
    assert gym_plan["calendar_arguments"]["time_zone"] == "Asia/Kolkata"


# ---------------------------------------------------------------------------
# Connection ownership and pending-state scoping
# ---------------------------------------------------------------------------


def test_gmail_pending_clarification_is_connection_scoped():
    now = datetime.now(timezone.utc)
    pending = {
        "version": 1,
        "type": "gmail_date_range_clarification",
        "user_id": "user-phase30",
        "conversation_id": "conversation-phase30",
        "connection_id": "conn-gmail-old",
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "period": "today",
        "time_zone": "Asia/Kolkata",
        "target_date": None,
    }

    result = gmail_agent_nodes._pending_gmail_clarification_resolution(
        _gmail_state(
            "yes",
            pending_gmail_clarification=pending,
            gmail_connection_id="conn-gmail-new",
        )
    )

    assert result == {
        "pending_gmail_clarification": None,
        "_gmail_clarification_clear_only": True,
    }


def test_contacts_pending_clarification_validator_is_connection_scoped():
    now = datetime.now(timezone.utc)
    pending = {
        "version": 1,
        "type": "contacts_read_clarification",
        "user_id": "user-phase30",
        "conversation_id": "conversation-phase30",
        "connection_id": "conn-contacts-old",
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "candidates": [{"resource_name": "people/mock-alex-001"}],
    }

    assert contacts_read._is_valid_pending_contacts_clarification(
        pending,
        user_id="user-phase30",
        conversation_id="conversation-phase30",
        connection_id="conn-contacts-old",
        now=now,
    )
    assert not contacts_read._is_valid_pending_contacts_clarification(
        pending,
        user_id="user-phase30",
        conversation_id="conversation-phase30",
        connection_id="conn-contacts-new",
        now=now,
    )


def test_chat_service_hydration_rejects_wrong_contacts_connection():
    now = datetime.now(timezone.utc)
    pending = {
        "version": 1,
        "type": "contacts_read_clarification",
        "user_id": "user-phase30",
        "conversation_id": "conversation-phase30",
        "connection_id": "conn-contacts-old",
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "candidates": [{"resource_name": "people/mock-alex-001"}],
    }
    history = [
        {
            "role": "assistant",
            "content": "Which Alex did you mean?",
            "metadata": {"pending_contacts_clarification": pending},
        }
    ]

    assert ChatService._pending_contacts_clarification(
        history,
        user_id=uuid4(),
        conversation_id=uuid4(),
        connection_id="conn-contacts-new",
        now=now,
    ) is None


# ---------------------------------------------------------------------------
# Contacts provenance and safe errors
# ---------------------------------------------------------------------------


class _FakeTool:
    def __init__(self, name, handler):
        self.name = name
        self._handler = handler

    async def ainvoke(self, args):
        return self._handler(args)


def _env_ok(data):
    return json.dumps({"ok": True, "data": data})


def _mock_contacts_tools(monkeypatch, handlers):
    tools = [_FakeTool(name, fn) for name, fn in handlers.items()]
    monkeypatch.setattr(
        contacts_read,
        "create_contacts_tools",
        lambda user_id, connection_id: tools,
    )
    monkeypatch.setattr(
        contacts_read,
        "extract_contacts_read_intent_llm",
        lambda message: None,
    )


def test_contacts_answer_uses_authoritative_people_api_data(monkeypatch):
    snippet = {
        "resource_name": "people/mock-alex-001",
        "display_name": "Alex",
        "primary_phone": "+1 555 999 0000",
        "organization": "Fake Corp",
    }
    authoritative = {
        "resource_name": "people/mock-alex-001",
        "display_name": "Alex",
        "primary_phone": "+8801700000123",
        "primary_email": "alex@trusted.example",
        "organization": "Trusted Org",
        "job_title": "Engineer",
        "addresses": [{"formatted": "Dhaka, Bangladesh", "primary": True}],
    }

    def search_contacts(_args):
        return _env_ok({"contacts": [snippet]})

    def get_contact(args):
        assert args["resource_name"] == authoritative["resource_name"]
        return _env_ok(authoritative)

    _mock_contacts_tools(
        monkeypatch,
        {
            "search_contacts": search_contacts,
            "list_contacts": lambda _args: _env_ok({}),
            "get_contact": get_contact,
        },
    )

    result = contacts_read.generate_contacts_read_response(
        user_id="user-phase30",
        connection_id="conn-contacts-phase30",
        message="Show details for Alex",
        conversation_id="conversation-phase30",
    )

    answer = result["assistant_response"]
    assert authoritative["primary_phone"] in answer
    assert authoritative["organization"] in answer
    assert snippet["primary_phone"] not in answer
    assert snippet["organization"] not in answer
    provenance = result["contacts_provenance"]
    assert provenance["verified_source"] is True
    assert provenance["outcome_status"] == "single_match"


def test_contacts_unverified_single_match_is_blocked(monkeypatch):
    async def fake_run(**kwargs):
        return (
            SimpleNamespace(),
            {
                "status": "single_match",
                "data_source": "untrusted_history",
                "selected_contact_returned_by_api": False,
                "matches": [{"display_name": "Alex", "primary_phone": "+1 555 999 0000"}],
            },
            None,
        )

    monkeypatch.setattr(contacts_read, "_run_contacts_read", fake_run)

    result = contacts_read.generate_contacts_read_response(
        user_id="user-phase30",
        connection_id="conn-contacts-phase30",
        message="Show Alex's phone number",
    )

    assert "+1 555 999 0000" not in result["assistant_response"]
    assert result["contacts_provenance"]["verified_source"] is False
    assert result["contacts_provenance"]["provenance_validation"] == "failed_unverified_single_match"


@pytest.mark.parametrize(
    ("error", "category", "fragment"),
    [
        (
            "Google returned PERMISSION_DENIED: caller lacks contacts scope for user@example.com",
            "permission_or_connection",
            "required",
        ),
        ("Request timed out", "timeout", "too long"),
        ("boom", "unavailable", "right now"),
    ],
)
def test_contacts_error_messages_do_not_echo_raw_upstream_text(monkeypatch, error, category, fragment):
    async def fake_run(**kwargs):
        return SimpleNamespace(), {"status": "error", "error": error}, None

    monkeypatch.setattr(contacts_read, "_run_contacts_read", fake_run)

    result = contacts_read.generate_contacts_read_response(
        user_id="user-phase30",
        connection_id="conn-contacts-phase30",
        message="Show my contacts",
    )

    answer = result["assistant_response"]
    assert error not in answer
    assert "user@example.com" not in answer
    assert fragment in answer
    assert result["contacts_provenance"]["error_category"] == category
    assert result["contacts_provenance"]["verified_source"] is False


# ---------------------------------------------------------------------------
# Streaming / transport parity
# ---------------------------------------------------------------------------


def test_sse_schema_matches_wire_format():
    event = ChatStreamEvent(
        type="replace",
        conversation_id="conversation-phase30",
        content="I can't verify personal Calendar events in a general response.",
    )

    wire = event.sse()
    assert wire.startswith("data: {")
    assert '"type":"replace"' in wire
    assert "event:" not in wire
    assert wire.endswith("\n\n")


def test_send_and_stream_share_the_same_generator(monkeypatch):
    from app.services import chat_service as chat_service_module

    captured = []

    def fake_generator(**kwargs):
        captured.append(kwargs)
        if kwargs["streaming"]:
            yield {"type": "token", "content": "grounded answer"}
        yield {
            "type": "done",
            "response": {"assistant_message": {"content": "grounded answer"}},
        }

    service = object.__new__(chat_service_module.ChatService)
    monkeypatch.setattr(service, "_send_message_generator", fake_generator)

    non_streaming = service.send_message(
        user_id=uuid4(),
        conversation_id=uuid4(),
        content="Show my contacts",
        idempotency_key=uuid4(),
    )
    streaming = list(
        service.stream_message(
            user_id=uuid4(),
            conversation_id=uuid4(),
            content="Show my contacts",
            idempotency_key=uuid4(),
        )
    )

    assert non_streaming["assistant_message"]["content"] == "grounded answer"
    assert [event["type"] for event in streaming] == ["token", "done"]
    assert [call["streaming"] for call in captured] == [False, True]
