import json
from types import SimpleNamespace

from app.llm.schemas import LLMResponse
from app.schemas.gmail_agent import (
    DraftEmailPlan,
    GmailReadPlan,
    GmailUnavailableActionPlan,
    SearchEmailsPlan,
    parse_assistant_intent,
)
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
