import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.schemas.gmail_agent import SummarizeImportantPlan
from app.workflows import gmail_agent_nodes, gmail_tools


USER_ID = UUID("00000000-0000-0000-0000-000000000001")
CONNECTION_ID = UUID("00000000-0000-0000-0000-000000000002")


def _state(message: str, **overrides):
    return {
        "user_message": message,
        "user_id": str(USER_ID),
        "conversation_id": "conversation-1",
        "assistant_response": "",
        "provider": "",
        "model": "",
        "messages": [],
        "user_timezone": "Asia/Kolkata",
        "request_time": "2026-11-20T10:00:00+05:30",
        **overrides,
    }


def _plan(message: str, **overrides):
    state = _state(message, **overrides)
    return gmail_agent_nodes._deterministic_read_plan(message, state=state)


def _query(plan: dict, **overrides):
    validated = SummarizeImportantPlan.model_validate_json(
        json.dumps(plan, ensure_ascii=False),
    )
    return gmail_tools.build_important_search_query(
        validated,
        user_timezone=overrides.get("user_timezone", "Asia/Kolkata"),
        request_time=overrides.get("request_time", "2026-11-20T10:00:00+05:30"),
    )


def _epoch(value: str) -> str:
    return str(int(datetime.fromisoformat(value).timestamp() * 1000))


def _list_message(message_id: str, when: str, **overrides):
    return {
        "id": message_id,
        "thread_id": f"thread-{message_id}",
        "internal_date": _epoch(when),
        "sender": "Alice <alice@example.com>",
        "recipient": "user@example.com",
        "subject": f"Subject {message_id}",
        "snippet": f"Snippet {message_id}",
        "labels": ["IMPORTANT"],
        **overrides,
    }


def _detail(message_id: str, when: str, *, body: str):
    return {
        "id": message_id,
        "thread_id": f"thread-{message_id}",
        "internal_date": _epoch(when),
        "headers": {
            "from": "Alice <alice@example.com>",
            "to": "user@example.com",
            "subject": f"Subject {message_id}",
            "date": "Fri, 20 Nov 2026 10:00:00 +0530",
        },
        "snippet": f"Snippet {message_id}",
        "labels": ["IMPORTANT"],
        "body": {"plain_text": body},
    }


class FakeGmailService:
    def __init__(self, pages, details):
        self.pages = list(pages)
        self.details = details
        self.list_calls = []
        self.detail_calls = []

    async def list_messages(self, **kwargs):
        self.list_calls.append(kwargs)
        if not self.pages:
            return {"messages": [], "next_page_token": None, "result_size_estimate": 0}
        return self.pages.pop(0)

    async def get_messages_for_summary(self, **kwargs):
        self.detail_calls.append(kwargs)
        return [
            self.details[message_id]
            for message_id in kwargs["message_ids"]
            if message_id in self.details
        ]


def test_bare_important_summary_plan_has_no_implicit_mailbox_or_time_filter():
    plan = _plan("Help me summarize my important emails.")

    assert plan["intent"] == "summarize_important"
    assert plan["scope"] == "all"
    assert plan["read_status"] == "any"
    assert plan["period"] == "any"
    assert plan["after"] is None
    assert plan["before"] is None
    query, details = _query(plan)
    assert query == "is:important"
    assert details["folder"] == "all"
    assert "newer_than" not in query
    assert "in:inbox" not in query


@pytest.mark.parametrize(
    ("message", "scope", "read_status", "sender", "recipient", "topic"),
    (
        ("Summarize important emails in my inbox", "inbox", "any", None, None, None),
        ("Summarize important emails in all mail", "all", "any", None, None, None),
        ("Show me my important unread emails", "all", "unread", None, None, None),
        (
            "Summarize important emails from boss@example.com",
            "all",
            "any",
            "boss@example.com",
            None,
            None,
        ),
        (
            "Summarize important emails sent to boss@example.com",
            "all",
            "any",
            None,
            "boss@example.com",
            None,
        ),
        (
            "Summarize important work-related emails",
            "all",
            "any",
            None,
            None,
            "work",
        ),
    ),
)
def test_natural_language_modifiers_are_preserved(
    message,
    scope,
    read_status,
    sender,
    recipient,
    topic,
):
    plan = _plan(message)

    assert plan["scope"] == scope
    assert plan["read_status"] == read_status
    assert plan.get("sender") == sender
    assert plan.get("recipient") == recipient
    assert plan.get("topic") == topic


def test_last_seven_days_uses_explicit_local_range_and_verifies_dates():
    plan = _plan("Summarize my important emails from the last 7 days")
    assert plan["period"] == "last_n_days"
    assert plan["window_days"] == 7

    query, details = _query(plan)
    assert "newer_than" not in query
    assert f"after:{int(details['start'].timestamp()) - 1}" in query
    assert f"before:{int(details['end'].timestamp())}" in query

    pages = [{
        "messages": [
            _list_message("too-old", "2026-11-01T05:00:00+00:00"),
            _list_message("in-range", "2026-11-20T05:00:00+00:00"),
        ],
        "next_page_token": None,
        "result_size_estimate": 2,
    }]
    details_by_id = {
        "too-old": _detail("too-old", "2026-11-01T05:00:00+00:00", body="Old"),
        "in-range": _detail(
            "in-range",
            "2026-11-20T05:00:00+00:00",
            body="Current authoritative body",
        ),
    }
    service = FakeGmailService(pages, details_by_id)

    result = asyncio.run(
        gmail_tools._execute_with_service(
            service=service,
            intent="summarize_important",
            plan=plan,
            user_id=USER_ID,
            connection_id=CONNECTION_ID,
            user_timezone="Asia/Kolkata",
            request_time="2026-11-20T10:00:00+05:30",
        )
    )

    assert [item["message_id"] for item in result["messages"]] == ["in-range"]
    assert result["verified_count"] == 1
    assert service.list_calls[0]["folder"] == "all"


def test_today_uses_request_timezone_boundary():
    plan = _plan(
        "Summarize important emails from today",
        request_time="2026-11-20T18:30:00+00:00",
        user_timezone="Asia/Kolkata",
    )
    assert plan["period"] == "today"
    assert "2026-11-21" in plan["period_label"]

    query, details = _query(
        plan,
        request_time="2026-11-20T18:30:00+00:00",
        user_timezone="Asia/Kolkata",
    )
    assert int(details["start"].timestamp()) == int(
        datetime(2026, 11, 20, 18, 30, tzinfo=timezone.utc).timestamp()
    )
    assert "before:" in query


def test_custom_date_range_is_preserved():
    plan = _plan("Summarize important emails from 2026-11-01 to 2026-11-07")

    assert plan["period"] == "custom"
    assert plan["after"] == "2026-11-01"
    assert plan["before"] == "2026-11-07"
    query, _ = _query(plan)
    assert query.startswith("is:important after:")
    assert " before:" in query


def test_all_mail_search_does_not_force_inbox_and_uses_authoritative_ids():
    plan = _plan("Summarize my important emails")
    pages = [{
        "messages": [_list_message("gmail-real-id", "2026-11-20T05:00:00+00:00")],
        "next_page_token": None,
        "result_size_estimate": 1,
    }]
    details_by_id = {
        "gmail-real-id": _detail(
            "gmail-real-id",
            "2026-11-20T05:00:00+00:00",
            body="Only retrieved content",
        ),
    }
    service = FakeGmailService(pages, details_by_id)

    result = asyncio.run(
        gmail_tools._execute_with_service(
            service=service,
            intent="summarize_important",
            plan=plan,
            user_id=USER_ID,
            connection_id=CONNECTION_ID,
        )
    )

    assert service.list_calls[0]["folder"] == "all"
    assert "in:inbox" not in service.list_calls[0]["query"]
    assert "-in:inbox" not in service.list_calls[0]["query"]
    assert result["search_criteria"] == "is:important"
    assert result["inbox_prioritized"] is False
    assert result["messages"][0]["message_id"] == "gmail-real-id"
    assert result["messages"][0]["body"] == "Only retrieved content"
    assert "Synthetic" not in str(result)
    assert service.detail_calls[0]["message_ids"] == ["gmail-real-id"]
    assert service.detail_calls[0]["user_id"] == USER_ID
    assert service.detail_calls[0]["connection_id"] == CONNECTION_ID


def test_pagination_collects_unique_authoritative_messages():
    plan = _plan("Summarize my important emails")
    pages = [
        {
            "messages": [_list_message("page-1", "2026-11-20T05:00:00+00:00")],
            "next_page_token": "token-2",
            "result_size_estimate": 2,
        },
        {
            "messages": [
                _list_message("page-1", "2026-11-20T05:00:00+00:00"),
                _list_message("page-2", "2026-11-20T06:00:00+00:00"),
            ],
            "next_page_token": None,
            "result_size_estimate": 2,
        },
    ]
    details_by_id = {
        message_id: _detail(
            message_id,
            "2026-11-20T05:00:00+00:00",
            body=f"Body {message_id}",
        )
        for message_id in ("page-1", "page-2")
    }
    service = FakeGmailService(pages, details_by_id)

    result = asyncio.run(
        gmail_tools._execute_with_service(
            service=service,
            intent="summarize_important",
            plan=plan,
            user_id=USER_ID,
            connection_id=CONNECTION_ID,
        )
    )

    assert len(service.list_calls) == 2
    assert service.list_calls[1]["page_token"] == "token-2"
    assert [item["message_id"] for item in result["messages"]] == [
        "page-2",
        "page-1",
    ]


def test_empty_important_search_response_is_truthful_without_fallback():
    state = _state("Help me summarize my important emails.")
    state["gmail_plan"] = {"intent": "summarize_important"}
    result = {
        "messages": [],
        "search_criteria": "is:important",
        "period": "unspecified (all dates)",
        "criteria": {"scope": "all", "read_status": "any"},
    }

    output = gmail_agent_nodes.gmail_response_node(state, result=result)

    assert "didn't find any important emails" in output["assistant_response"].lower()
    assert "gmail query: is:important" in output["assistant_response"].lower()
    assert "pending_gmail_clarification" not in output


def test_summary_prompt_grounds_llm_in_retrieved_messages_only(monkeypatch):
    captured = {}

    def fake_generate(request):
        captured["request"] = request
        return SimpleNamespace(
            content="Grounded summary.",
            provider="test-provider",
            model="test-model",
            completion_metadata=lambda: {"provider": "test-provider"},
        )

    monkeypatch.setattr(gmail_agent_nodes, "generate_or_stream", fake_generate)
    state = _state("Summarize my important emails.")
    state["gmail_plan"] = {"intent": "summarize_important"}
    result = {
        "messages": [
            {
                "message_id": "gmail-real-id",
                "sender": "Alice <alice@example.com>",
                "subject": "Real subject",
                "date": "Fri, 20 Nov 2026 10:00:00 +0530",
                "body": "Authoritative body text",
            },
        ],
        "search_criteria": "is:important",
        "period": "unspecified (all dates)",
    }

    output = gmail_agent_nodes.gmail_response_node(state, result=result)

    assert output["assistant_response"] == "Grounded summary."
    prompt = "\n".join(
        message.content for message in captured["request"].messages
    )
    assert "Authoritative body text" in prompt
    assert "Never invent senders" in prompt
