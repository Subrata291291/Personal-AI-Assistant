import json
from types import SimpleNamespace

from app.llm.schemas import LLMResponse
from app.schemas.gmail_agent import (
    DraftEmailPlan,
    GmailReadPlan,
    GmailUnavailableActionPlan,
    IntegrationPlan,
    SearchEmailsPlan,
    parse_assistant_intent,
)
from app.workflows.chat_nodes import capabilities_node
from app.workflows import gmail_agent_nodes


SUBSCRIPTION_REQUEST = (
    "Send a mail to [nanditabagchi260@gmail.com]"
    "(mailto:nanditabagchi260@gmail.com) to take Personal AI subscription "
    "from this month. Add beautiful features of it so that the user takes "
    "the subscription."
)


def _state(message, **overrides):
    return {
        "user_message": message,
        "user_id": "user-1",
        "conversation_id": "conversation-1",
        "assistant_response": "",
        "provider": "",
        "model": "",
        "messages": [],
        **overrides,
    }


def test_subscription_email_with_markdown_recipient_is_composition_even_with_stale_searches():
    state = _state(
        SUBSCRIPTION_REQUEST,
        available_gmail_sources={
            "messages": [
                {"message_id": "old-message-1", "thread_id": "old-thread-1"},
                {"message_id": "old-message-2", "thread_id": "old-thread-2"},
            ],
        },
    )

    routed = gmail_agent_nodes.gmail_intent_router_node(state)
    plan = DraftEmailPlan.model_validate(routed["gmail_plan"])

    assert routed["assistant_route"] == "gmail"
    assert plan.intent == "gmail_compose"
    assert plan.to == ["nanditabagchi260@gmail.com"]
    assert plan.source_message_id is None
    assert plan.source_thread_id is None
    assert "beautiful features" in " ".join(plan.content_requirements).casefold()


def test_write_and_create_email_requests_classify_as_compose():
    for text in (
        "Write an email to [user@example.com](mailto:user@example.com) and "
        "explain our product features.",
        "Create a professional proposal email.",
        "Email John about tomorrow's meeting.",
    ):
        plan = gmail_agent_nodes._deterministic_assistant_plan(
            _state(text),
        )[0]
        assert plan["intent"] == "gmail_compose"
        assert plan["draft_type"] == "new"


def test_explicit_draft_send_search_read_reply_archive_and_delete_intents():
    cases = (
        ("Save this email in Gmail Drafts.", "gmail_save_draft"),
        ("Send this email after I approve it.", "gmail_send"),
        ("Find emails from user@example.com about subscriptions.", "search_emails"),
        ("Archive this email.", "gmail_archive"),
        ("Delete this email.", "gmail_delete"),
    )
    for text, expected in cases:
        if expected == "search_emails":
            plan = gmail_agent_nodes.gmail_intent_router_node(
                _state(text),
            )["gmail_plan"]
        else:
            plan = gmail_agent_nodes._deterministic_assistant_plan(
                _state(text),
            )[0]
        assert plan["intent"] == expected

    assert isinstance(
        parse_assistant_intent(json.dumps({
            "intent": "gmail_read",
            "message_id": "message-123",
        })),
        GmailReadPlan,
    )
    assert isinstance(
        parse_assistant_intent(json.dumps({
            "intent": "gmail_search",
            "query": "subscription",
        })),
        SearchEmailsPlan,
    )
    assert isinstance(
        parse_assistant_intent(json.dumps({
            "intent": "gmail_delete",
        })),
        GmailUnavailableActionPlan,
    )


def test_capability_questions_use_a_deterministic_capability_route():
    for text in (
        "What can you do?",
        "What can you help me with?",
        "What are your capabilities?",
    ):
        routed = gmail_agent_nodes.gmail_intent_router_node(_state(text))
        assert routed["assistant_route"] == "capabilities"
        assert routed["gmail_plan"]["intent"] == "capabilities"


def test_connected_gmail_capability_response_is_runtime_specific_and_safe():
    capabilities = gmail_agent_nodes.build_runtime_capabilities(
        {
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.modify",
            "https://www.googleapis.com/auth/gmail.send",
        },
        {"google_calendar", "google_drive", "google_contacts", "tasks"},
    )
    result = capabilities_node(_state(
        "What can you do?",
        assistant_capabilities=capabilities,
    ))
    answer = result["assistant_response"]

    assert "search, read, and summarize" in answer
    assert "editable new-email and reply/forward draft previews" in answer
    assert "Save Draft" in answer
    assert "explicit approval" in answer
    assert "never send automatically" in answer
    assert "Google Calendar, Google Drive, Google Contacts, Tasks and reminders" in answer
    assert "cannot directly access, read, modify, or send emails" not in answer


def test_disconnected_gmail_capability_response_explains_how_to_connect():
    capabilities = gmail_agent_nodes.build_runtime_capabilities(
        set(),
        {"google_calendar", "google_drive", "google_contacts"},
    )
    result = capabilities_node(_state(
        "What can you do?",
        assistant_capabilities=capabilities,
    ))

    assert "Gmail search and reading are not available" in result["assistant_response"]
    assert "Connect or select your Google account in Connected Apps" in result["assistant_response"]
    assert "Google Calendar, Google Drive, Google Contacts operations are not implemented yet" in result["assistant_response"]
    assert "search, read, and summarize messages" not in result["assistant_response"]


def test_calendar_tools_are_reported_when_scopes_and_route_are_available():
    capabilities = gmail_agent_nodes.build_runtime_capabilities(
        set(),
        set(),
        calendar_capabilities={
            "connected": True,
            "events_read": True,
            "freebusy": True,
            "api_available": True,
            "api_complete": True,
        },
    )
    result = capabilities_node(_state(
        "What can you do?",
        assistant_capabilities=capabilities,
    ))

    assert capabilities["google_calendar"] == {
        "connected": True,
        "google_connection_available": False,
        "events_read": True,
        "freebusy": True,
        "api_available": True,
        "api_complete": True,
        "chat_available": True,
        "selection_required": False,
        "default_connection_invalid": False,
        "default_connection_selected": False,
        "selected_event_scope_missing": False,
        "selected_freebusy_scope_missing": False,
        "write_available": False,
    }
    assert "read-only event search" in result["assistant_response"]
    assert "free/busy checks" in result["assistant_response"]


def test_unconnected_calendar_is_not_reported_as_chat_or_api_available():
    capabilities = gmail_agent_nodes.build_runtime_capabilities(
        set(),
        {"google_calendar"},
    )
    result = capabilities_node(_state(
        "What can you do?",
        assistant_capabilities=capabilities,
    ))

    assert capabilities["google_calendar"]["connected"] is False
    assert capabilities["google_calendar"]["api_available"] is False
    assert capabilities["google_calendar"]["api_complete"] is False
    assert capabilities["google_calendar"]["chat_available"] is False
    assert "Google Calendar is not connected" in result["assistant_response"]
    assert "Google Calendar operations are not implemented yet" in result["assistant_response"]


def test_calendar_api_grant_can_exist_while_chat_tools_are_unavailable():
    capabilities = gmail_agent_nodes.build_runtime_capabilities(
        set(),
        {"google_calendar"},
        calendar_capabilities={
            "connected": True,
            "events_read": True,
            "freebusy": False,
            "api_available": True,
            "api_complete": False,
        },
    )
    result = capabilities_node(_state(
        "What can you do?",
        assistant_capabilities=capabilities,
    ))

    assert capabilities["google_calendar"]["api_available"] is True
    assert capabilities["google_calendar"]["chat_available"] is False
    assert "grant event reading through the Calendar API" in (
        result["assistant_response"]
    )
    assert "Your connected Google accounts currently grant event reading" in (
        result["assistant_response"]
    )


def test_google_account_without_calendar_grants_is_not_called_disconnected():
    capabilities = gmail_agent_nodes.build_runtime_capabilities(
        set(),
        set(),
        calendar_capabilities={
            "connected": False,
            "google_connection_available": True,
            "events_read": False,
            "freebusy": False,
            "api_available": False,
            "api_complete": False,
        },
    )
    result = capabilities_node(_state(
        "What can you do?",
        assistant_capabilities=capabilities,
    ))

    assert "A Google account is connected, but Calendar permissions have not been granted" in (
        result["assistant_response"]
    )


def test_calendar_grants_on_separate_accounts_are_reported_as_aggregated():
    capabilities = gmail_agent_nodes.build_runtime_capabilities(
        set(),
        set(),
        calendar_capabilities={
            "connected": True,
            "events_read": True,
            "freebusy": True,
            "api_available": True,
            "api_complete": True,
        },
    )
    result = capabilities_node(_state(
        "What can you do?",
        assistant_capabilities=capabilities,
    ))

    assert capabilities["google_calendar"]["api_available"] is True
    assert capabilities["google_calendar"]["api_complete"] is True
    assert "read-only event search" in result["assistant_response"]
    assert "free/busy checks" in result["assistant_response"]


def test_mixed_gmail_and_calendar_request_asks_which_task_to_do_first():
    state = _state(
        "Show my calendar meetings and unread Gmail messages."
    )

    routed = gmail_agent_nodes.gmail_intent_router_node(state)
    assert routed["assistant_route"] == "gmail"
    assert routed["gmail_plan"]["clarification_reason"] == (
        "mixed_gmail_calendar"
    )

    response = gmail_agent_nodes.gmail_agent_node(
        {**state, "gmail_plan": routed["gmail_plan"]},
        SimpleNamespace(context={"user_id": "user-1"}),
    )
    assert "Which task would you like me to do first?" in (
        response["assistant_response"]
    )


def test_gmail_search_about_meetings_does_not_get_routed_to_calendar():
    routed = gmail_agent_nodes.gmail_intent_router_node(
        _state("Find emails about my meetings tomorrow.")
    )

    assert routed["assistant_route"] == "gmail"
    assert routed["gmail_plan"]["intent"] == "search_emails"


def test_common_calendar_requests_route_to_read_only_calendar_tools():
    cases = (
        ("Show my upcoming meetings.", "list_calendar_events"),
        ("What is my schedule today?", "get_today_schedule"),
        ("Show my meetings tomorrow.", "get_calendar_schedule"),
        ("Show my weekly schedule.", "get_calendar_schedule"),
        ("Find my next meeting with John.", "list_calendar_events"),
        ("Am I free tomorrow at 3 PM?", "check_calendar_availability"),
    )
    for text, expected_tool in cases:
        routed = gmail_agent_nodes.gmail_intent_router_node(_state(text))
        assert routed["assistant_route"] == "google_calendar"
        assert routed["gmail_plan"]["intent"] == "google_calendar"
        assert routed["gmail_plan"]["calendar_tool"] == expected_tool


def test_calendar_availability_without_a_complete_interval_clarifies():
    routed = gmail_agent_nodes.gmail_intent_router_node(
        _state("Am I free tomorrow?")
    )
    assert routed["assistant_route"] == "google_calendar"
    assert routed["gmail_plan"]["calendar_tool"] == "clarify"


def test_calendar_availability_respects_explicit_iana_timezone_and_dst():
    explicit_zone = gmail_agent_nodes.gmail_intent_router_node(
        _state(
            "Am I free on 2026-03-08 at 3 PM America/Los_Angeles?",
            user_timezone="UTC",
        )
    )
    nonexistent_local_time = gmail_agent_nodes.gmail_intent_router_node(
        _state(
            "Am I free on 2026-03-08 at 2:30 AM "
            "America/Los_Angeles?",
            user_timezone="UTC",
        )
    )
    ambiguous_local_time = gmail_agent_nodes.gmail_intent_router_node(
        _state(
            "Am I free on 2026-11-01 at 1:30 AM "
            "America/Los_Angeles?",
            user_timezone="UTC",
        )
    )
    abbreviation = gmail_agent_nodes.gmail_intent_router_node(
        _state("Am I free tomorrow at 3 PM PST?")
    )

    assert explicit_zone["gmail_plan"]["calendar_arguments"]["time_zone"] == (
        "America/Los_Angeles"
    )
    assert explicit_zone["gmail_plan"]["calendar_arguments"]["time_min"].endswith(
        "-07:00"
    )
    assert nonexistent_local_time["gmail_plan"]["calendar_tool"] == "clarify"
    assert ambiguous_local_time["gmail_plan"]["calendar_tool"] == "clarify"
    assert abbreviation["gmail_plan"]["calendar_tool"] == "clarify"


def test_llm_calendar_plans_validate_as_strict_tool_intents():
    plan = parse_assistant_intent(json.dumps({
        "intent": "google_calendar",
        "calendar_tool": "get_calendar_schedule",
        "calendar_arguments": {
            "date": "2026-11-05",
            "period": "week",
            "time_zone": "UTC",
        },
    }))
    assert isinstance(plan, IntegrationPlan)
    assert plan.intent == "google_calendar"
    assert plan.calendar_tool == "get_calendar_schedule"
    assert plan.calendar_arguments["period"] == "week"


def test_calendar_changes_are_never_planned_as_read_operations():
    routed = gmail_agent_nodes.gmail_intent_router_node(
        _state("Schedule a meeting with John tomorrow.")
    )
    assert routed["assistant_route"] == "google_calendar"
    assert routed["gmail_plan"]["calendar_tool"] == "clarify"


def test_gmail_and_general_requests_keep_existing_routing():
    gmail = gmail_agent_nodes.gmail_intent_router_node(
        _state("Find emails from Alex about the project.")
    )
    general = gmail_agent_nodes.gmail_intent_router_node(
        _state("Explain how photosynthesis works.")
    )
    assert gmail["assistant_route"] == "gmail"
    assert gmail["gmail_plan"]["intent"] == "search_emails"
    assert general["assistant_route"] == "general"


def test_compose_without_recipient_requests_only_the_recipient(monkeypatch):
    text = "Create a professional proposal email about our new product."
    raw_plan = gmail_agent_nodes._deterministic_assistant_plan(
        _state(text),
    )[0]
    response = LLMResponse(
        content=json.dumps({
            "draft_type": "new",
            "to": [],
            "cc": [],
            "bcc": [],
            "subject": "Proposal for Our New Product",
            "body": "Hello,\n\nI would like to introduce our new product.",
            "source_message_id": None,
            "source_thread_id": None,
        }),
        provider="test",
        model="test-model",
        latency_ms=0,
        finish_reason="stop",
    )
    monkeypatch.setattr(gmail_agent_nodes.llm_router, "generate", lambda _request: response)

    result = gmail_agent_nodes._create_email_draft(
        _state(text, gmail_plan=raw_plan),
        SimpleNamespace(context={"user_id": "user-1"}),
        raw_plan,
    )

    assert "What recipient email address should I use?" in result["assistant_response"]
    assert result["email_draft"]["to"] == []
    assert result["email_draft"]["subject"] == "Proposal for Our New Product"


def test_compose_plan_does_not_enter_gmail_search_clarification():
    state = _state(
        "Write a proposal email to user@example.com about our services.",
        available_gmail_sources={
            "messages": [
                {"message_id": "old-1", "thread_id": "thread-1"},
                {"message_id": "old-2", "thread_id": "thread-2"},
            ],
        },
    )

    result = gmail_agent_nodes.gmail_intent_router_node(state)

    assert result["gmail_plan"]["intent"] == "gmail_compose"
    assert result["gmail_plan"]["intent"] != "clarify"


def test_gmail_read_and_search_aliases_map_to_existing_read_tools(monkeypatch):
    captured = []
    async def fake_read(**kwargs):
        captured.append(kwargs)
        return {"messages": []}

    monkeypatch.setattr(gmail_agent_nodes, "execute_gmail_read", fake_read)
    runtime = SimpleNamespace(
        context={
            "user_id": "user-1",
            "gmail_connection_id": "connection-1",
        },
    )
    for plan in (
        {"intent": "gmail_read", "message_id": "known-message"},
        {"intent": "gmail_read", "thread_id": "known-thread"},
        {"intent": "gmail_search", "query": "subscription"},
    ):
        gmail_agent_nodes.gmail_agent_node(
            _state("", gmail_plan=plan),
            runtime,
        )

    assert [call["intent"] for call in captured] == [
        "get_email",
        "get_thread",
        "search_emails",
    ]
    search_plan = gmail_agent_nodes._deterministic_read_plan(
        "Find emails from user@example.com about subscriptions.",
    )
    assert search_plan["intent"] == "search_emails"
    assert search_plan["sender"] == "user@example.com"
    assert search_plan["max_results"] == 10


def test_send_intent_still_only_enters_editable_draft_flow(monkeypatch):
    called = []

    def fake_create_draft(state, runtime, plan):
        called.append((state, runtime, plan))
        return {"assistant_response": "Editable draft.", "email_draft": {}}

    monkeypatch.setattr(gmail_agent_nodes, "_create_email_draft", fake_create_draft)
    state = _state(
        "Send an email to user@example.com.",
        gmail_plan={
            "intent": "gmail_send",
            "draft_type": "new",
            "to": ["user@example.com"],
        },
    )
    runtime = SimpleNamespace(context={"user_id": "user-1"})

    result = gmail_agent_nodes.gmail_agent_node(state, runtime)

    assert result["assistant_response"] == "Editable draft."
    assert called and called[0][2]["intent"] == "gmail_send"


def test_reply_and_forward_intents_require_a_known_source():
    source = {
        "messages": [
            {"message_id": "known-message", "thread_id": "known-thread"},
        ],
    }
    for text, expected_intent in (
        ("Reply to that email.", "gmail_reply"),
        ("Reply all to that email.", "gmail_reply_all"),
        ("Forward that email.", "gmail_forward"),
    ):
        plan = gmail_agent_nodes._deterministic_draft_plan(
            _state(text, available_gmail_sources=source),
        )

        assert plan["intent"] == expected_intent
        assert plan["source_message_id"] == "known-message"
        assert plan["source_thread_id"] == "known-thread"
