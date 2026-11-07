import json
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.llm.schemas import LLMResponse
from app.schemas.gmail_agent import (
    DraftEmailPlan,
    GmailReadPlan,
    GmailUnavailableActionPlan,
    IntegrationPlan,
    SearchEmailsPlan,
    parse_assistant_intent,
)
from app.workflows.chat_nodes import (
    capabilities_node,
    integration_unavailable_node,
    llm_node,
)
from app.workflows import chat_nodes
from app.workflows import gmail_agent_nodes
from app.workflows.calendar_agent_node import calendar_agent_node


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


def test_pending_calendar_time_range_revision_preserves_event_details():
    state = _state(
        "Change it to 2 PM to 3:30 PM.",
        assistant_capabilities={
            "google_calendar": {"write_permission_granted": True},
        },
        pending_calendar_create_proposal={
            "proposal_id": "original-proposal",
            "parameters": {
                "calendar_id": "primary",
                "summary": "Gym",
                "description": "Training",
                "start": "2026-11-08T10:00:00+05:30",
                "end": "2026-11-08T11:00:00+05:30",
                "time_zone": "Asia/Kolkata",
                "location": "Fitness center",
                "attendees": [],
                "all_day": False,
            },
        },
    )

    plan, route = gmail_agent_nodes._deterministic_assistant_plan(state)

    assert route == "google_calendar"
    assert plan["calendar_tool"] == "create_calendar_event_proposal"
    assert plan["supersedes_proposal_id"] == "original-proposal"
    assert plan["calendar_arguments"] == {
        "calendar_id": "primary",
        "summary": "Gym",
        "description": "Training",
        "start": "2026-11-08T14:00:00+05:30",
        "end": "2026-11-08T15:30:00+05:30",
        "time_zone": "Asia/Kolkata",
        "location": "Fitness center",
        "attendees": [],
        "all_day": False,
    }


def test_pending_calendar_single_endpoint_revision_keeps_other_time():
    state = _state(
        "Change the start time to 9 AM.",
        assistant_capabilities={
            "google_calendar": {"write_permission_granted": True},
        },
        pending_calendar_create_proposal={
            "proposal_id": "original-proposal",
            "parameters": {
                "calendar_id": "primary",
                "summary": "Gym",
                "description": None,
                "start": "2026-11-08T10:00:00+05:30",
                "end": "2026-11-08T11:00:00+05:30",
                "time_zone": "Asia/Kolkata",
                "location": None,
                "attendees": [],
                "all_day": False,
            },
        },
    )

    plan, route = gmail_agent_nodes._deterministic_assistant_plan(state)

    assert route == "google_calendar"
    assert plan["calendar_arguments"]["start"] == (
        "2026-11-08T09:00:00+05:30"
    )
    assert plan["calendar_arguments"]["end"] == (
        "2026-11-08T11:00:00+05:30"
    )
    assert plan["supersedes_proposal_id"] == "original-proposal"


def test_pending_calendar_single_time_without_endpoint_clarifies():
    state = _state(
        "Change it to 9 AM.",
        pending_calendar_create_proposal={
            "proposal_id": "original-proposal",
            "parameters": {
                "calendar_id": "primary",
                "summary": "Gym",
                "description": None,
                "start": "2026-11-08T10:00:00+05:30",
                "end": "2026-11-08T11:00:00+05:30",
                "time_zone": "Asia/Kolkata",
                "location": None,
                "attendees": [],
                "all_day": False,
            },
        },
    )

    plan, route = gmail_agent_nodes._deterministic_assistant_plan(state)

    assert route == "google_calendar"
    assert plan["calendar_tool"] == "clarify"


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


def test_calendar_and_gmail_requests_route_to_their_service():
    cases = (
        (
            "I want to add a meeting in my calendar today at 11 PM with my friend.",
            "google_calendar",
            "create_calendar_event_proposal",
        ),
        (
            "Add an event to my Google Calendar tomorrow.",
            "google_calendar",
            "create_calendar_event_proposal",
        ),
        (
            "Schedule a meeting today at 11 PM.",
            "google_calendar",
            "create_calendar_event_proposal",
        ),
        (
            "Book an appointment in my calendar tomorrow at 9 AM.",
            "google_calendar",
            "create_calendar_event_proposal",
        ),
        (
            "What is on my Google Calendar today?",
            "google_calendar",
            "get_today_schedule",
        ),
        (
            "Show my calendar this month.",
            "google_calendar",
            "list_calendar_events",
        ),
        (
            "Show my Google Calendar this month.",
            "google_calendar",
            "list_calendar_events",
        ),
        (
            "What meetings do I have tomorrow?",
            "google_calendar",
            "get_calendar_schedule",
        ),
        (
            "Find my events for next week.",
            "google_calendar",
            "get_calendar_schedule",
        ),
        (
            "Am I free tomorrow between 2 PM and 4 PM?",
            "google_calendar",
            "check_calendar_availability",
        ),
        (
            "Show details of my next meeting.",
            "google_calendar",
            "list_calendar_events",
        ),
        (
            "Move my meeting to tomorrow.",
            "google_calendar",
            "update_calendar_event_proposal",
        ),
        (
            "Delete my calendar event.",
            "google_calendar",
            "delete_calendar_event_proposal",
        ),
        ("Search my emails.", "gmail", "search_emails"),
        ("Find emails from Google.", "gmail", "search_emails"),
    )
    for text, expected_route, expected_tool in cases:
        routed = gmail_agent_nodes.gmail_intent_router_node(_state(
            text,
            user_timezone="America/Los_Angeles",
            request_time=datetime(
                2026, 11, 4, 17, 0, tzinfo=ZoneInfo("UTC")
            ),
        ))
        assert routed["assistant_route"] == expected_route
        if expected_route == "google_calendar":
            assert routed["gmail_plan"]["calendar_tool"] == expected_tool
            assert routed["gmail_plan"]["intent"] == "google_calendar"
            if text == "Show details of my next meeting.":
                assert routed["gmail_plan"]["calendar_arguments"]["max_results"] == 10
                assert routed["gmail_plan"]["calendar_arguments"]["time_min"] == (
                    "2026-11-04T09:00:00-08:00"
                )
            if text == "Am I free tomorrow between 2 PM and 4 PM?":
                assert routed["gmail_plan"]["calendar_arguments"]["time_zone"] == "America/Los_Angeles"
        else:
            assert routed["gmail_plan"]["intent"] == expected_tool


def test_calendar_create_request_is_never_sent_to_gmail_or_claimed_successful():
    text = (
        "I want to add a meeting in my calendar today at 11 PM with my friend."
    )
    routed = gmail_agent_nodes.gmail_intent_router_node(_state(
        text,
        user_timezone="America/Los_Angeles",
        request_time=datetime(2026, 11, 4, 17, 0, tzinfo=ZoneInfo("UTC")),
    ))
    response = calendar_agent_node(
        {
            **_state(text),
            "gmail_plan": routed["gmail_plan"],
        },
        SimpleNamespace(context={
            "calendar_enabled": True,
            "google_connection_available": True,
        }),
    )["assistant_response"]

    assert routed["assistant_route"] == "google_calendar"
    assert routed["gmail_plan"]["calendar_tool"] == "create_calendar_event_proposal"
    assert "write permission" in response.casefold()
    assert "nothing was created" in response.casefold()
    assert "created successfully" not in response.casefold()
    assert "gmail_search" not in str(routed["gmail_plan"])


def test_calendar_create_intents_short_circuit_without_requesting_more_details():
    cases = (
        "Add a meeting to my calendar today at 11 PM.",
        "Create a calendar event.",
        "Book an appointment in my calendar.",
    )
    for text in cases:
        routed = gmail_agent_nodes.gmail_intent_router_node(_state(
            text,
            user_timezone="Asia/Kolkata",
            request_time=datetime(
                2026, 11, 6, 14, 30, tzinfo=ZoneInfo("UTC")
            ),
        ))
        result = calendar_agent_node(
            {
                **_state(text),
                "gmail_plan": routed["gmail_plan"],
                "assistant_capabilities": {
                    "google_calendar": {
                        "write_execution_available": False,
                    },
                },
            },
            SimpleNamespace(context={
                "calendar_enabled": True,
                "google_connection_available": True,
            }),
        )

        assert routed["assistant_route"] == "google_calendar"
        assert routed["gmail_plan"]["calendar_tool"] == (
            "create_calendar_event_proposal"
        )
        assert "write permission" in result["assistant_response"].casefold()
        assert "nothing was created" in result["assistant_response"].casefold()
        assert "calendar_create_event_proposal" not in result


def test_calendar_create_request_when_disconnected_reports_connection_state():
    text = "Add a meeting to my calendar today at 11 PM."
    routed = gmail_agent_nodes.gmail_intent_router_node(_state(
        text,
        user_timezone="Asia/Kolkata",
        request_time=datetime(
            2026, 11, 6, 14, 30, tzinfo=ZoneInfo("UTC")
        ),
    ))
    result = calendar_agent_node(
        {
            **_state(text),
            "gmail_plan": routed["gmail_plan"],
        },
        SimpleNamespace(context={
            "calendar_enabled": True,
            "google_connection_available": False,
        }),
    )
    assert routed["assistant_route"] == "google_calendar"
    assert result["assistant_response"] == (
        "Google Calendar is not connected. Connect a Google account in "
        "Connected Apps. No event was created."
    )
    assert "calendar_create_event_proposal" not in result


def test_calendar_month_plan_uses_request_time_and_selected_timezone():
    routed = gmail_agent_nodes.gmail_intent_router_node(_state(
        "Show my calendar this month.",
        user_timezone="America/Los_Angeles",
        request_time=datetime(2026, 11, 4, 17, 0, tzinfo=ZoneInfo("UTC")),
    ))
    arguments = routed["gmail_plan"]["calendar_arguments"]

    assert routed["assistant_route"] == "google_calendar"
    assert routed["gmail_plan"]["calendar_tool"] == "list_calendar_events"
    assert arguments["time_min"] == "2026-11-01T00:00:00-07:00"
    assert arguments["time_max"] == "2026-12-01T00:00:00-08:00"
    assert arguments["time_zone"] == "America/Los_Angeles"


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
        "write_permission_granted": False,
        "write_execution_available": False,
        "create_execution_available": False,
        "update_execution_available": False,
        "delete_execution_available": False,
        "create_feature_enabled": False,
        "update_feature_enabled": False,
        "delete_feature_enabled": False,
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


def test_drive_and_contact_requests_route_to_explicit_unavailable_results():
    cases = (
        ("Find a PDF in my Drive.", "google_drive"),
        ("Find Subrata's phone number.", "google_contacts"),
    )
    for message, expected_route in cases:
        state = _state(message)
        routed = gmail_agent_nodes.gmail_intent_router_node(state)
        assert routed["assistant_route"] == expected_route

        result = integration_unavailable_node({
            **state,
            **routed,
        })
        assert "not implemented in the Assistant yet" in (
            result["assistant_response"]
        )
        assert "cannot retrieve or change that data" in (
            result["assistant_response"]
        )


def test_mixed_drive_and_contacts_request_clarifies_without_provider_calls():
    state = _state("Find a PDF in my Drive and Subrata's phone number.")
    routed = gmail_agent_nodes.gmail_intent_router_node(state)

    assert routed["assistant_route"] == "gmail"
    assert routed["gmail_plan"]["clarification_reason"] == (
        "mixed_google_services"
    )
    result = gmail_agent_nodes.gmail_agent_node(
        {**state, "gmail_plan": routed["gmail_plan"]},
        SimpleNamespace(context={"user_id": "user-1"}),
    )
    assert "multiple Google services" in result["assistant_response"]


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


def test_calendar_relative_dates_use_one_request_time_in_user_timezone():
    request_time = "2026-11-06T18:45:00+00:00"
    today = gmail_agent_nodes.gmail_intent_router_node(
        _state(
            "What's on my calendar today?",
            user_timezone="Asia/Kolkata",
            request_time=request_time,
        )
    )
    tomorrow = gmail_agent_nodes.gmail_intent_router_node(
        _state(
            "What events do I have tomorrow?",
            user_timezone="Asia/Kolkata",
            request_time=request_time,
        )
    )

    assert today["assistant_route"] == "google_calendar"
    assert today["gmail_plan"]["calendar_tool"] == "get_today_schedule"
    assert tomorrow["gmail_plan"]["calendar_arguments"]["date"] == "2026-11-08"
    assert tomorrow["gmail_plan"]["calendar_arguments"]["time_zone"] == (
        "Asia/Kolkata"
    )


def test_calendar_date_ranges_afternoon_search_and_implicit_today_freebusy():
    date_range = gmail_agent_nodes.gmail_intent_router_node(
        _state(
            "Show my calendar from 2026-11-06 to 2026-11-08.",
            user_timezone="Asia/Kolkata",
            request_time="2026-11-06T18:45:00+00:00",
        )
    )["gmail_plan"]
    afternoon = gmail_agent_nodes.gmail_intent_router_node(
        _state(
            "Do I have any meetings this afternoon?",
            user_timezone="Asia/Kolkata",
            request_time="2026-11-06T04:30:00+00:00",
        )
    )["gmail_plan"]
    availability = gmail_agent_nodes.gmail_intent_router_node(
        _state(
            "Am I free between 2 PM and 4 PM?",
            user_timezone="Asia/Kolkata",
            request_time="2026-11-06T04:30:00+00:00",
        )
    )["gmail_plan"]
    title_search = gmail_agent_nodes.gmail_intent_router_node(
        _state("Find my meeting titled product review.")
    )["gmail_plan"]

    assert date_range["calendar_tool"] == "list_calendar_events"
    assert date_range["calendar_arguments"]["time_min"].startswith(
        "2026-11-06T00:00:00"
    )
    assert date_range["calendar_arguments"]["time_max"].startswith(
        "2026-11-09T00:00:00"
    )
    assert afternoon["calendar_arguments"]["time_min"].startswith(
        "2026-11-06T12:00:00"
    )
    assert afternoon["calendar_arguments"]["time_max"].startswith(
        "2026-11-06T17:00:00"
    )
    assert availability["calendar_tool"] == "check_calendar_availability"
    assert availability["calendar_arguments"]["time_min"].startswith(
        "2026-11-06T14:00:00"
    )
    assert title_search["calendar_arguments"]["query"] == "product review"


def test_calendar_route_ignores_stale_conversation_events_and_clarifies():
    stale_event = "2024-11-06 Old product launch meeting"
    routed = gmail_agent_nodes.gmail_intent_router_node(
        _state(
            "What's on my calendar today?",
            messages=[{"role": "assistant", "content": stale_event}],
            user_timezone="Asia/Kolkata",
            request_time="2026-11-06T18:45:00+00:00",
        )
    )

    assert routed["assistant_route"] == "google_calendar"
    assert routed["gmail_plan"]["calendar_tool"] == "get_today_schedule"
    assert stale_event not in json.dumps(routed)


def test_general_assistant_questions_do_not_route_to_calendar():
    routed = gmail_agent_nodes.gmail_intent_router_node(
        _state("Explain how time zones work.")
    )
    assert routed["assistant_route"] == "general"


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


def test_contextual_calendar_followup_reaches_planner_with_bounded_history(
    monkeypatch,
):
    response = LLMResponse(
        content=(
            '{"intent":"google_calendar","calendar_tool":"get_calendar_schedule",'
            '"calendar_arguments":{"date":"2026-11-05","period":"week",'
            '"time_zone":"UTC"}}'
        ),
        provider="test",
        model="test-model",
        latency_ms=0,
        finish_reason="stop",
    )
    calls = []
    monkeypatch.setattr(
        gmail_agent_nodes.llm_router,
        "generate",
        lambda request: calls.append(request) or response,
    )

    routed = gmail_agent_nodes.gmail_intent_router_node(
        _state(
            "Move it to 4 PM",
            messages=[
                {"role": "user", "content": "Schedule a meeting with Rahul tomorrow."},
                {"role": "assistant", "content": "What time should I use?"},
            ],
        )
    )

    assert routed["assistant_route"] == "google_calendar"
    assert routed["gmail_plan"]["calendar_tool"] == "get_calendar_schedule"
    assert len(calls) == 1
    planner_input = calls[0].messages[-1].content
    assert "Schedule a meeting with Rahul tomorrow." in planner_input
    assert "Current user request to classify:\nMove it to 4 PM" in planner_input
    assert "infer approval" in calls[0].messages[0].content


def test_unrelated_short_followup_does_not_invoke_integration_planner(monkeypatch):
    calls = []
    monkeypatch.setattr(
        gmail_agent_nodes.llm_router,
        "generate",
        lambda request: calls.append(request),
    )

    routed = gmail_agent_nodes.gmail_intent_router_node(
        _state(
            "Make it 4 PM",
            messages=[
                {"role": "user", "content": "I prefer a quiet workspace."},
                {"role": "assistant", "content": "Understood."},
            ],
        )
    )

    assert routed["assistant_route"] == "general"
    assert calls == []


def test_calendar_changes_route_to_approval_proposals_never_read_operations():
    cases = (
        ("Move my meeting to tomorrow.", "update_calendar_event_proposal"),
        ("Delete my calendar event.", "delete_calendar_event_proposal"),
        ("Update my calendar meeting.", "update_calendar_event_proposal"),
    )
    for text, expected_tool in cases:
        routed = gmail_agent_nodes.gmail_intent_router_node(_state(text))
        assert routed["assistant_route"] == "google_calendar"
        assert routed["gmail_plan"]["calendar_tool"] == expected_tool


def test_calendar_update_with_write_permission_routes_through_planner_when_flag_off(
    monkeypatch,
):
    response = LLMResponse(
        content=json.dumps({
            "intent": "google_calendar",
            "calendar_tool": "update_calendar_event_proposal",
            "calendar_arguments": {
                "calendar_id": "primary",
                "event_query": "gym session",
                "changes": {"summary": "Gym session"},
            },
        }),
        provider="test",
        model="test-model",
        latency_ms=0,
        finish_reason="stop",
    )
    calls = []
    monkeypatch.setattr(
        gmail_agent_nodes.llm_router,
        "generate",
        lambda request: calls.append(request) or response,
    )

    routed = gmail_agent_nodes.gmail_intent_router_node(_state(
        "Move my gym session to 5 PM.",
        assistant_capabilities={
            "google_calendar": {
                "write_permission_granted": True,
                "update_execution_available": False,
            },
        },
    ))

    assert len(calls) == 1
    assert routed["assistant_route"] == "google_calendar"
    assert routed["gmail_plan"]["calendar_tool"] == (
        "update_calendar_event_proposal"
    )


def test_calendar_create_without_complete_details_asks_for_clarification(
    monkeypatch,
):
    response = LLMResponse(
        content=json.dumps({
            "intent": "google_calendar",
            "calendar_tool": "create_calendar_event_proposal",
            "calendar_arguments": {
                "calendar_id": "primary",
                "summary": "Design review",
                "start": "2026-06-10T10:00:00-07:00",
                "end": "2026-06-10T11:00:00-07:00",
                "time_zone": "America/Los_Angeles",
                "all_day": False,
            },
        }),
        provider="test",
        model="test-model",
        latency_ms=0,
        finish_reason="stop",
    )
    calls = []
    monkeypatch.setattr(
        gmail_agent_nodes.llm_router,
        "generate",
        lambda request: calls.append(request) or response,
    )

    routed = gmail_agent_nodes.gmail_intent_router_node(_state(
        "Schedule a meeting with John tomorrow.",
        assistant_capabilities={
            "google_calendar": {
                "write_permission_granted": True,
                "create_execution_available": False,
            },
        },
    ))

    assert calls == []
    assert routed["assistant_route"] == "google_calendar"
    assert routed["gmail_plan"]["calendar_tool"] == (
        "create_calendar_event_proposal"
    )
    assert routed["gmail_plan"]["calendar_arguments"] is None


def test_gym_event_creation_clarifies_missing_date_and_resolves_followup():
    request_time = datetime(
        2026, 11, 6, 14, 30, tzinfo=ZoneInfo("UTC")
    )
    capabilities = {
        "google_calendar": {
            "write_permission_granted": True,
            "create_execution_available": False,
            "write_execution_available": False,
        },
    }
    initial = _state(
        "add in my calendar gym timing morning 10am to 11am",
        user_timezone="Asia/Kolkata",
        request_time=request_time,
        assistant_capabilities=capabilities,
    )
    initial_plan = gmail_agent_nodes.gmail_intent_router_node(initial)
    assert initial_plan["assistant_route"] == "google_calendar"
    assert initial_plan["gmail_plan"]["calendar_tool"] == (
        "create_calendar_event_proposal"
    )
    assert initial_plan["gmail_plan"]["calendar_arguments"] is None

    followup = _state(
        "Tomorrow",
        messages=[
            {"role": "user", "content": initial["user_message"]},
            {
                "role": "assistant",
                "content": (
                    "What date should I schedule this event for, "
                    "10am–11am?"
                ),
            },
        ],
        user_timezone="Asia/Kolkata",
        request_time=request_time,
        assistant_capabilities=capabilities,
    )
    followup_plan = gmail_agent_nodes.gmail_intent_router_node(followup)

    assert followup_plan["assistant_route"] == "google_calendar"
    assert followup_plan["gmail_plan"]["calendar_arguments"] == {
        "calendar_id": "primary",
        "summary": "Gym",
        "description": None,
        "start": "2026-11-07T10:00:00+05:30",
        "end": "2026-11-07T11:00:00+05:30",
        "time_zone": "Asia/Kolkata",
        "location": None,
        "attendees": [],
        "all_day": False,
    }


@pytest.mark.parametrize(
    ("input_text", "start", "end"),
    [
        (
            "Add gym in my calendar from 10 AM to 11 AM tomorrow.",
            "2026-11-08T10:00:00+05:30",
            "2026-11-08T11:00:00+05:30",
        ),
        (
            "Add gym in my calendar from 2 PM to 3:30 PM tomorrow.",
            "2026-11-08T14:00:00+05:30",
            "2026-11-08T15:30:00+05:30",
        ),
        (
            "Add gym in my calendar from 14:00 to 15:00 tomorrow.",
            "2026-11-08T14:00:00+05:30",
            "2026-11-08T15:00:00+05:30",
        ),
    ],
)
def test_calendar_create_preserves_explicit_12_and_24_hour_times(
    input_text,
    start,
    end,
):
    routed = gmail_agent_nodes.gmail_intent_router_node(_state(
        input_text,
        user_timezone="Asia/Kolkata",
        request_time=datetime(2026, 11, 7, 6, 0, tzinfo=ZoneInfo("UTC")),
        assistant_capabilities={
            "google_calendar": {"write_permission_granted": True},
        },
    ))

    parameters = routed["gmail_plan"]["calendar_arguments"]
    assert parameters["summary"] == "Gym"
    assert parameters["start"] == start
    assert parameters["end"] == end
    assert parameters["time_zone"] == "Asia/Kolkata"


def test_calendar_create_time_change_followup_preserves_date_and_title():
    history = [
        {
            "role": "user",
            "content": "Add gym in my calendar from 10 AM to 11 AM",
        },
        {
            "role": "assistant",
            "content": "What date should I schedule this event for, 10 AM–11 AM?",
        },
        {
            "role": "user",
            "content": "Actually change it to 2 PM to 3:30 PM",
        },
        {
            "role": "assistant",
            "content": "What date should I schedule this event for, 2 PM–3:30 PM?",
        },
    ]
    routed = gmail_agent_nodes.gmail_intent_router_node(_state(
        "Tomorrow",
        messages=history,
        user_timezone="Asia/Kolkata",
        request_time=datetime(2026, 11, 7, 6, 0, tzinfo=ZoneInfo("UTC")),
        assistant_capabilities={
            "google_calendar": {"write_permission_granted": True},
        },
    ))

    parameters = routed["gmail_plan"]["calendar_arguments"]
    assert parameters["summary"] == "Gym"
    assert parameters["start"] == "2026-11-08T14:00:00+05:30"
    assert parameters["end"] == "2026-11-08T15:30:00+05:30"
    assert parameters["time_zone"] == "Asia/Kolkata"


def test_calendar_create_single_time_followup_preserves_other_endpoint():
    history = [
        {
            "role": "user",
            "content": "Add gym in my calendar from 10 AM to 11 AM",
        },
        {
            "role": "assistant",
            "content": "What date should I schedule this event for, 10 AM–11 AM?",
        },
        {
            "role": "user",
            "content": "Change the end time to 12 PM",
        },
        {
            "role": "assistant",
            "content": "What date should I schedule this event for, 10 AM–12 PM?",
        },
    ]
    routed = gmail_agent_nodes.gmail_intent_router_node(_state(
        "Tomorrow",
        messages=history,
        user_timezone="Asia/Kolkata",
        request_time=datetime(2026, 11, 7, 6, 0, tzinfo=ZoneInfo("UTC")),
        assistant_capabilities={
            "google_calendar": {"write_permission_granted": True},
        },
    ))

    parameters = routed["gmail_plan"]["calendar_arguments"]
    assert parameters["start"] == "2026-11-08T10:00:00+05:30"
    assert parameters["end"] == "2026-11-08T12:00:00+05:30"


def test_calendar_create_without_end_time_asks_for_clarification():
    routed = gmail_agent_nodes.gmail_intent_router_node(_state(
        "Add gym in my calendar tomorrow at 10 AM.",
        user_timezone="Asia/Kolkata",
        assistant_capabilities={
            "google_calendar": {"write_permission_granted": True},
        },
    ))

    assert routed["gmail_plan"]["calendar_tool"] == (
        "create_calendar_event_proposal"
    )
    assert routed["gmail_plan"]["calendar_arguments"] is None


def test_calendar_create_requires_explicit_overnight_for_cross_midnight():
    capabilities = {
        "google_calendar": {"write_permission_granted": True},
    }
    ambiguous = gmail_agent_nodes.gmail_intent_router_node(_state(
        "Add gym in my calendar from 10 PM to 2 AM tomorrow.",
        user_timezone="Asia/Kolkata",
        assistant_capabilities=capabilities,
    ))
    overnight = gmail_agent_nodes.gmail_intent_router_node(_state(
        "Add gym in my calendar from 10 PM to 2 AM overnight tomorrow.",
        user_timezone="Asia/Kolkata",
        assistant_capabilities=capabilities,
    ))

    assert ambiguous["gmail_plan"]["calendar_tool"] == "clarify"
    assert overnight["gmail_plan"]["calendar_arguments"]["start"] == (
        "2026-11-08T22:00:00+05:30"
    )
    assert overnight["gmail_plan"]["calendar_arguments"]["end"] == (
        "2026-11-09T02:00:00+05:30"
    )


def test_calendar_all_day_request_has_date_without_timed_values():
    routed = gmail_agent_nodes.gmail_intent_router_node(_state(
        "Add gym in my calendar all day tomorrow.",
        user_timezone="Asia/Kolkata",
        assistant_capabilities={
            "google_calendar": {"write_permission_granted": True},
        },
    ))

    parameters = routed["gmail_plan"]["calendar_arguments"]
    assert parameters["all_day"] is True
    assert parameters["start"] == "2026-11-08"
    assert parameters["end"] == "2026-11-09"
    assert parameters["time_zone"] is None


def test_calendar_capability_response_reports_disabled_execution_flags():
    state = _state(
        "What can you do?",
        assistant_capabilities={
            "google_calendar": {
                "connected": True,
                "api_available": True,
                "chat_available": True,
                "events_read": True,
                "freebusy": True,
                "write_permission_granted": True,
                "create_execution_available": False,
                "update_execution_available": False,
                "delete_execution_available": False,
            },
        },
    )

    routed = gmail_agent_nodes.gmail_intent_router_node(state)
    response = capabilities_node({**state, **routed})

    assert routed["assistant_route"] == "capabilities"
    assert "read-only event search" in response["assistant_response"]
    assert "Calendar write permission is granted" in response["assistant_response"]
    assert "create execution: disabled" in response["assistant_response"]
    assert "update execution: disabled" in response["assistant_response"]
    assert "delete execution: disabled" in response["assistant_response"]


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


def test_general_response_cannot_present_unverified_calendar_facts(monkeypatch):
    monkeypatch.setattr(
        chat_nodes.llm_router,
        "generate",
        lambda request: SimpleNamespace(
            content="You have a meeting at 2 PM from your old calendar.",
            provider="test",
            model="test-model",
            completion_metadata=lambda: {},
        ),
    )

    result = llm_node(_state(
        "Explain photosynthesis.",
        assistant_route="general",
        messages=[{
            "role": "assistant",
            "content": "Your 2024-11-06 calendar included a design review.",
        }],
    ))

    assert result["assistant_response"] == (
        "I can't verify personal Calendar events in a general response. "
        "Ask me about your calendar for a live check."
    )
    assert result["messages"][-1]["content"] == result["assistant_response"]


def test_general_response_keeps_unrelated_answers(monkeypatch):
    content = "Photosynthesis converts light energy into chemical energy."
    monkeypatch.setattr(
        chat_nodes.llm_router,
        "generate",
        lambda request: SimpleNamespace(
            content=content,
            provider="test",
            model="test-model",
            completion_metadata=lambda: {},
        ),
    )

    result = llm_node(_state(
        "Explain photosynthesis.",
        assistant_route="general",
    ))

    assert result["assistant_response"] == content


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
