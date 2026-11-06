import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api.deps import (
    is_integration_enabled,
    require_integration_enabled,
)
from app.services.chat_service import ChatService
from app.workflows import calendar_agent_node, gmail_agent_nodes


def test_integration_preferences_default_to_enabled_for_existing_users():
    assert is_integration_enabled({}, "gmail") is True
    assert is_integration_enabled(
        {"user_metadata": {"calendar_enabled": True}},
        "calendar",
    ) is True
    assert is_integration_enabled(
        {"user_metadata": {"gmail_enabled": False}},
        "gmail",
    ) is False


@pytest.mark.parametrize("integration", ["gmail", "calendar"])
def test_disabled_integration_is_rejected_by_api_guard(integration):
    guard = require_integration_enabled(integration)

    with pytest.raises(HTTPException) as error:
        asyncio.run(guard({
            "user_metadata": {f"{integration}_enabled": False},
        }))

    assert error.value.status_code == 403
    assert integration.title() in error.value.detail


def test_chat_idempotency_hash_includes_disabled_integration_state():
    conversation_id = uuid4()
    enabled_hash = ChatService._generate_request_hash(
        conversation_id,
        "show calendar",
    )
    disabled_hash = ChatService._generate_request_hash(
        conversation_id,
        "show calendar",
        calendar_enabled=False,
    )

    assert enabled_hash != disabled_hash


def test_disabled_calendar_chat_does_not_invoke_calendar_tools():
    result = calendar_agent_node.calendar_agent_node(
        {
            "user_message": "Show my schedule today.",
            "gmail_plan": {
                "calendar_tool": "get_today_schedule",
                "calendar_arguments": {
                    "time_zone": "UTC",
                    "calendar_id": "primary",
                },
            },
        },
        SimpleNamespace(context={"calendar_enabled": False}),
    )

    assert "turned off in Connected Apps" in result["assistant_response"]


def test_disabled_gmail_chat_does_not_invoke_gmail_services():
    result = gmail_agent_nodes.gmail_agent_node(
        {
            "user_message": "Find emails from Alex.",
            "gmail_plan": {
                "intent": "search_emails",
            },
        },
        SimpleNamespace(context={
            "gmail_enabled": False,
            "gmail_connection_id": str(uuid4()),
            "user_id": str(uuid4()),
        }),
    )

    assert "turned off in Connected Apps" in result["assistant_response"]
