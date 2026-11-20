import asyncio
import logging
from datetime import datetime
from uuid import UUID

import pytest

from app.workflows import gmail_agent_nodes, gmail_tools


USER_ID = UUID("00000000-0000-0000-0000-000000000001")
CONNECTION_ID = UUID("00000000-0000-0000-0000-000000000002")


def _state(message: str, **overrides):
    return {
        "user_message": message,
        "user_id": "user-1",
        "conversation_id": "conversation-1",
        "assistant_response": "",
        "provider": "",
        "model": "",
        "messages": [],
        "user_timezone": "Asia/Kolkata",
        "request_time": "2026-11-20T10:00:00+05:30",
        **overrides,
    }


def _message(message_id: str, internal_date: str, **overrides):
    return {
        "id": message_id,
        "thread_id": f"thread-{message_id}",
        "internal_date": internal_date,
        "sender": "Sender <sender@example.com>",
        "recipient": "user@example.com",
        "subject": f"Subject {message_id}",
        "snippet": f"Snippet {message_id}",
        "labels": ["INBOX"],
        **overrides,
    }


def _detail(message_id: str, *, body="Real full body"):
    return {
        "id": message_id,
        "thread_id": f"thread-{message_id}",
        "internal_date": "1763000000000",
        "headers": {
            "from": "Sender <sender@example.com>",
            "to": "user@example.com",
            "subject": f"Subject {message_id}",
            "date": "Fri, 20 Nov 2026 10:00:00 +0530",
        },
        "body": {"plain_text": body},
        "labels": ["INBOX"],
    }


@pytest.mark.parametrize(
    ("message", "folder"),
    (
        ("Check my last mail", "all"),
        ("Show me my last email", "all"),
        ("Show my latest received email", "all"),
        ("What was the latest email I received?", "all"),
        ("Summarize my latest email", "all"),
        ("Check my last sent mail", "sent"),
        ("Check the last mail I have sent", "sent"),
        ("What was the latest email I sent?", "sent"),
    ),
)
def test_latest_mail_phrases_route_deterministically(message, folder):
    routed = gmail_agent_nodes.gmail_intent_router_node(_state(message))

    assert routed["assistant_route"] == "gmail"
    plan = routed["gmail_plan"]
    assert plan["intent"] == "get_latest_email"
    assert plan["folder"] == folder
    assert "query" not in plan


def test_latest_received_email_does_not_misroute_to_contacts(monkeypatch):
    def fail_llm(*args, **kwargs):
        raise AssertionError("Clear latest-mail request must not use LLM fallback")

    monkeypatch.setattr(gmail_agent_nodes.llm_router, "generate", fail_llm)

    routed = gmail_agent_nodes.gmail_intent_router_node(
        _state("Show my latest received email")
    )

    assert routed["assistant_route"] == "gmail"
    assert routed["gmail_plan"]["intent"] == "get_latest_email"


def test_send_and_draft_requests_have_correct_action_metadata():
    send_plan = gmail_agent_nodes._deterministic_assistant_plan(
        _state("Send a test email to subrata291291@gmail.com.")
    )[0]
    draft_plan = gmail_agent_nodes._deterministic_assistant_plan(
        _state("Draft an email to subrata291291@gmail.com.")
    )[0]

    assert send_plan["intent"] == "gmail_compose"
    assert send_plan["requested_action"] == "send"
    assert send_plan["to"] == ["subrata291291@gmail.com"]
    assert "query" not in send_plan

    assert draft_plan["intent"] == "gmail_compose"
    assert draft_plan["requested_action"] == "compose"
    assert draft_plan["to"] == ["subrata291291@gmail.com"]


@pytest.mark.parametrize(
    "message",
    (
        "Delete this event",
        "Delete Gym time from my Google Calendar",
        "Remove tomorrow's gym event",
        "Cancel my meeting at 9 AM",
        "Delete the event I just created",
        "Remove the last event we created",
    ),
)
def test_calendar_delete_phrases_never_route_to_gmail(message):
    routed = gmail_agent_nodes._deterministic_assistant_plan(_state(message))

    assert routed is not None
    plan, route = routed
    assert route == "google_calendar"
    assert plan["calendar_tool"] == "delete_calendar_event_proposal"
    assert "query" not in plan


def test_latest_received_uses_authoritative_id_and_excludes_sent(caplog):
    calls = []

    class FakeGmailService:
        async def list_messages(self, **kwargs):
            calls.append(("list", kwargs))
            return {
                "messages": [
                    _message(
                        "received-old",
                        "1700000000000",
                        sender="Sender <sender@example.com>",
                    ),
                    _message(
                        "sent-newest",
                        "1800000000000",
                        labels=["SENT"],
                        sender="user@example.com",
                    ),
                    _message(
                        "received-newest",
                        "1750000000000",
                        sender="Sender <sender@example.com>",
                    ),
                ],
                "next_page_token": None,
                "result_size_estimate": 3,
            }

        async def get_message(self, **kwargs):
            calls.append(("get", kwargs))
            return _detail(kwargs["message_id"], body="SECRET_FULL_BODY")

    with caplog.at_level(logging.INFO, logger="app.workflows.gmail_tools"):
        result = asyncio.run(
            gmail_tools._execute_with_service(
                service=FakeGmailService(),
                intent="get_latest_email",
                plan={"intent": "get_latest_email", "folder": "all"},
                user_id=USER_ID,
                connection_id=CONNECTION_ID,
            )
        )

    list_kwargs = [kwargs for kind, kwargs in calls if kind == "list"][0]
    get_kwargs = [kwargs for kind, kwargs in calls if kind == "get"]
    assert list_kwargs["folder"] == "all"
    assert list_kwargs["query"] == "-from:me"
    assert list_kwargs["user_id"] == USER_ID
    assert list_kwargs["connection_id"] == CONNECTION_ID
    assert [kwargs["message_id"] for kwargs in get_kwargs] == ["received-newest"]
    assert result["message"]["message_id"] == "received-newest"
    assert result["gmail_sources"]["messages"][0]["message_id"] == "received-newest"
    assert result["message"]["body"] == "SECRET_FULL_BODY"

    log_text = caplog.text
    assert "received-newest" in log_text
    assert "SECRET_FULL_BODY" not in log_text


def test_latest_sent_uses_sent_mailbox_and_authoritative_id():
    calls = []

    class FakeGmailService:
        async def list_messages(self, **kwargs):
            calls.append(kwargs)
            return {
                "messages": [
                    _message(
                        "received-newest",
                        "1800000000000",
                        labels=["INBOX"],
                    ),
                    _message(
                        "sent-older",
                        "1740000000000",
                        labels=["SENT"],
                    ),
                    _message(
                        "sent-newest",
                        "1750000000000",
                        labels=["SENT"],
                    ),
                ],
                "next_page_token": None,
                "result_size_estimate": 3,
            }

        async def get_message(self, **kwargs):
            return _detail(kwargs["message_id"])

    result = asyncio.run(
        gmail_tools._execute_with_service(
            service=FakeGmailService(),
            intent="get_latest_email",
            plan={"intent": "get_latest_email", "folder": "sent"},
            user_id=USER_ID,
            connection_id=CONNECTION_ID,
        )
    )

    assert calls[0]["folder"] == "sent"
    assert result["message"]["message_id"] == "sent-newest"
    assert result["gmail_sources"]["messages"][0]["message_id"] == "sent-newest"


def test_today_query_uses_injected_kolkata_boundaries_and_verifies_timestamps():
    captured = {}
    detail_calls = []
    start_epoch = int(
        datetime.fromisoformat("2026-11-19T18:30:00+00:00").timestamp()
    )
    end_epoch = int(
        datetime.fromisoformat("2026-11-20T18:30:00+00:00").timestamp()
    )

    class FakeGmailService:
        async def list_messages(self, **kwargs):
            captured.update(kwargs)
            return {
                "messages": [
                    _message("before-midnight", f"{(start_epoch - 60) * 1000}"),
                    _message("after-midnight", f"{(start_epoch + 60) * 1000}"),
                    _message("at-end", f"{end_epoch * 1000}"),
                    _message("malformed", "not-a-timestamp"),
                ],
                "next_page_token": None,
                "result_size_estimate": 4,
            }

        async def get_messages_for_summary(self, **kwargs):
            detail_calls.append(kwargs)
            return [
                _detail(message_id)
                for message_id in kwargs["message_ids"]
            ]

        async def get_message(self, **kwargs):
            raise AssertionError("Unexpected single-message detail fallback")

    result = asyncio.run(
        gmail_tools._execute_with_service(
            service=FakeGmailService(),
            intent="summarize_today",
            plan={"intent": "summarize_today", "max_results": 10},
            user_id=USER_ID,
            connection_id=CONNECTION_ID,
            user_timezone="Asia/Kolkata",
            request_time="2026-11-20T10:00:00+05:30",
        )
    )

    assert captured["query"] == (
        f"after:{start_epoch - 1} before:{end_epoch} -from:me"
    )
    assert result["local_day_start"] == "2026-11-20T00:00:00+05:30"
    assert result["local_day_end"] == "2026-11-21T00:00:00+05:30"
    assert detail_calls[0]["message_ids"] == ["after-midnight"]
    assert [item["message_id"] for item in result["messages"]] == [
        "after-midnight"
    ]
    assert result["messages"][0]["body"] == "Real full body"
    assert result["messages"][0]["sender"] == "Sender <sender@example.com>"
    assert all(item["message_id"] for item in result["gmail_sources"]["messages"])


def test_latest_lookup_follows_bounded_pagination():
    calls = []

    class FakeGmailService:
        async def list_messages(self, **kwargs):
            calls.append(kwargs.get("page_token"))
            if kwargs.get("page_token") is None:
                return {
                    "messages": [_message("page-one", "1700000000000")],
                    "next_page_token": "page-2",
                    "result_size_estimate": 2,
                }
            return {
                "messages": [
                    _message("page-two-newest", "1800000000000"),
                ],
                "next_page_token": None,
                "result_size_estimate": 2,
            }

        async def get_message(self, **kwargs):
            return _detail(kwargs["message_id"])

    result = asyncio.run(
        gmail_tools._execute_with_service(
            service=FakeGmailService(),
            intent="get_latest_email",
            plan={"intent": "get_latest_email", "folder": "all"},
            user_id=USER_ID,
            connection_id=CONNECTION_ID,
        )
    )

    assert calls == [None, "page-2"]
    assert result["message"]["message_id"] == "page-two-newest"


def test_empty_latest_lookup_never_fabricates_a_message():
    class FakeGmailService:
        async def list_messages(self, **kwargs):
            return {
                "messages": [],
                "next_page_token": None,
                "result_size_estimate": 0,
            }

        async def get_message(self, **kwargs):
            raise AssertionError("No message ID means no full-message fetch")

    result = asyncio.run(
        gmail_tools._execute_with_service(
            service=FakeGmailService(),
            intent="get_latest_email",
            plan={"intent": "get_latest_email", "folder": "all"},
            user_id=USER_ID,
            connection_id=CONNECTION_ID,
        )
    )

    assert result["message"] is None
    assert result["result_size_estimate"] == 0
    assert result["gmail_sources"]["messages"] == []
