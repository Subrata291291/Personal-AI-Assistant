"""Phase 32: independent end-to-end Gmail grounding regressions.

Every test uses deterministic mocks and synthetic fixtures. No live Google API,
live LLM provider, credential, `.env`, OAuth scope, mutation, or deployment is
involved.
"""

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

from app.api.v1.endpoints import chat as chat_endpoint
from app.llm.schemas import LLMResponse
from app.schemas.chat_stream import ChatStreamEvent
from app.workflows import gmail_agent_nodes, gmail_long_response, gmail_tools


USER_ID = UUID("11111111-1111-1111-1111-111111111111")
CONNECTION_ID = UUID("44444444-4444-4444-4444-444444444444")
REQUEST_TIME = "2026-11-20T12:00:00Z"


def _llm_response(content, *, finish_reason="stop"):
    return LLMResponse(
        content=content,
        provider="mock",
        model="mock-model",
        finish_reason=finish_reason,
        latency_ms=0,
    )


def _state(message="Check all my today's mail and summarize them step by step."):
    return {
        "user_message": message,
        "user_id": "user-phase32",
        "conversation_id": "conversation-phase32",
        "assistant_response": "",
        "provider": "",
        "model": "",
        "messages": [],
        "user_timezone": "UTC",
        "gmail_connection_id": str(CONNECTION_ID),
        "request_time": REQUEST_TIME,
        "gmail_plan": {"intent": "summarize_today"},
    }


def _normalized(message_id, body):
    return gmail_long_response.normalize_summary_message({
        "message_id": message_id,
        "thread_id": f"thread-{message_id}",
        "sender": "sender@example.com",
        "subject": f"Subject {message_id}",
        "date": "Fri, 20 Nov 2026 09:00:00 +0000",
        "body": body,
    })


def test_cap_result_recomputes_counts_ids_remaining_and_sources():
    ids = [f"m{index}" for index in range(12)]
    result = {
        "messages": [
            {"message_id": message_id, "body": "x" * 3000}
            for message_id in ids
        ],
        "retrieved_count": len(ids),
        "processed_count": len(ids),
        "retrieved_message_ids": list(ids),
        "failed_message_ids": ["failed-id"],
        "remaining_count": 0,
        "has_more": False,
        "content_truncated": False,
        "gmail_sources": {
            "connection_id": str(CONNECTION_ID),
            "messages": [
                {"message_id": message_id, "thread_id": ""}
                for message_id in ids
            ],
        },
    }

    capped = gmail_tools._cap_result(result)
    displayed = [
        message["message_id"]
        for message in capped["messages"]
        if isinstance(message, dict)
    ]
    assert 0 < len(displayed) < len(ids)
    assert capped["content_truncated"] is True
    assert capped["retrieved_count"] == len(displayed)
    assert capped["processed_count"] == len(displayed)
    assert set(capped["retrieved_message_ids"]) == set(displayed)
    assert capped["remaining_count"] >= len(ids) - len(displayed)
    assert capped["has_more"] is True
    source_ids = {
        record.get("message_id")
        for record in capped["gmail_sources"]["messages"]
    }
    assert source_ids <= set(displayed)


def test_bounded_pagination_reports_has_more_after_page_cap_exhaustion():
    class ShortPages:
        def __init__(self):
            self.calls = 0

        async def list_messages(self, **_kwargs):
            self.calls += 1
            return {
                "messages": [{"id": f"p{self.calls}"}],
                "result_size_estimate": 0,
                "next_page_token": f"token-{self.calls}",
            }

    service = ShortPages()
    collected, estimates, has_more = asyncio.run(
        gmail_tools._collect_message_pages_bounded(
            service=service,
            user_id=USER_ID,
            connection_id=CONNECTION_ID,
            query=None,
            folder="all",
            page_size=10,
            max_pages=2,
            target_count=10,
        )
    )

    assert len(collected) == 2
    assert estimates == 0
    assert has_more is True


def test_today_out_of_window_messages_are_not_retrieval_failures():
    in_window_ms = int(
        datetime(2026, 11, 20, 6, 0, tzinfo=timezone.utc).timestamp() * 1000
    )
    stale_ms = int(
        datetime(2026, 11, 19, 20, 0, tzinfo=timezone.utc).timestamp() * 1000
    )

    class FakeService:
        async def list_messages(self, **_kwargs):
            return {
                "messages": [
                    {"id": "stale", "internal_date": str(stale_ms)},
                    {"id": "fresh", "internal_date": str(in_window_ms)},
                ],
                "result_size_estimate": 0,
                "next_page_token": None,
            }

        async def get_messages_for_summary(self, *, message_ids, **_kwargs):
            return [
                {
                    "id": message_id,
                    "internal_date": str(in_window_ms),
                    "headers": {"from": "a@example.com", "subject": "Today"},
                    "body": {"plain_text": "in window"},
                }
                for message_id in message_ids
            ]

    result = asyncio.run(
        gmail_tools._execute_with_service(
            service=FakeService(),
            intent="summarize_today",
            plan={"intent": "summarize_today", "max_results": 5},
            user_id=USER_ID,
            connection_id=CONNECTION_ID,
            user_timezone="UTC",
            request_time=REQUEST_TIME,
        )
    )

    assert result["candidate_count"] == 1
    assert result["failed_count"] == 0
    assert "stale" not in result["failed_message_ids"]
    assert [message["message_id"] for message in result["messages"]] == ["fresh"]


def test_compacted_message_records_body_truncation():
    compact = gmail_tools._compact_message({
        "id": "m1",
        "headers": {"subject": "Long message"},
        "body": {"plain_text": "x" * 5000},
    })

    assert compact["body_truncated"] is True
    assert len(compact["body"]) == gmail_tools.MAX_EMAIL_BODY_CHARS


def test_empty_today_result_with_remaining_does_not_claim_no_mail(monkeypatch):
    monkeypatch.setattr(
        gmail_agent_nodes.llm_router,
        "generate",
        Mock(side_effect=AssertionError("must not summarize empty results")),
    )
    output = gmail_agent_nodes.gmail_response_node(
        _state(),
        result={
            "messages": [],
            "failed_count": 0,
            "remaining_count": 3,
            "has_more": True,
            "requested_count": 5,
            "period": "today",
        },
    )

    answer = output["assistant_response"].lower()
    assert "no incoming email" not in answer
    assert "remain" in answer or "couldn't confirm" in answer
    metadata = output["response_metadata"]
    assert metadata["status"] == "partial"
    assert metadata["remaining_item_count"] == 3


def test_short_summary_discloses_retrieval_failures(monkeypatch):
    monkeypatch.setattr(
        gmail_agent_nodes.llm_router,
        "generate",
        Mock(return_value=_llm_response("Here are all your emails today.")),
    )
    monkeypatch.setattr(gmail_agent_nodes, "emit_replace", Mock())

    messages = [
        _normalized("m1", "First email body."),
        _normalized("m2", "Second email body."),
    ]
    output = gmail_agent_nodes.gmail_response_node(
        _state(),
        result={
            "messages": messages,
            "failed_count": 40,
            "remaining_count": 40,
            "retrieved_count": 2,
            "requested_count": 42,
            "period": "today",
        },
    )

    answer = output["assistant_response"].lower()
    assert "40" in answer
    assert "not retrieved" in answer or "could not be retrieved" in answer
    metadata = output["response_metadata"]
    assert metadata["status"] == "partial"
    assert metadata["failed_item_count"] == 40
    assert metadata["remaining_item_count"] == 40


def test_batch_summaries_are_validated_per_message():
    batch = [
        _normalized("m1", "Please review the Q3 report."),
        _normalized("m2", "Meeting with Acme Corp about Project Phoenix."),
    ]
    content = json.dumps({
        "summaries": [
            {
                "message_id": "m1",
                "summary": "Meeting with Acme Corp about Project Phoenix.",
                "action": "None stated",
                "deadline": "Not specified",
            },
            {
                "message_id": "m2",
                "summary": "Please review the Q3 report.",
                "action": "None stated",
                "deadline": "Not specified",
            },
        ]
    })

    verified = gmail_long_response.extract_verified_batch_summaries(
        content,
        batch,
        gmail_agent_nodes._gmail_summary_is_grounded,
    )

    # Neither swapped summary is grounded in that message's own body. A
    # batch-wide validator would wrongly accept both by pooling tokens.
    assert verified == {}


def test_validator_rejects_unsupported_numeric_claim_from_subject_only():
    evidence = {
        "messages": [
            {
                "message_id": "m1",
                "subject": "Q4 Financial Results and Bonus Announcement",
                "body": "",
                "snippet": "",
            }
        ]
    }
    content = json.dumps({
        "summaries": [
            {
                "message_id": "m1",
                "summary": "The company announced a 20% bonus for all employees.",
                "action": "None stated",
                "deadline": "Not specified",
            }
        ]
    })

    assert not gmail_agent_nodes._gmail_summary_is_grounded(content, evidence)


def test_attachment_metadata_is_preserved_and_claims_are_checked():
    compact = gmail_tools._compact_message({
        "id": "m1",
        "headers": {"subject": "Invoice"},
        "body": {"plain_text": "Please pay."},
        "attachments": [
            {
                "filename": "Invoice-2026.pdf",
                "mime_type": "application/pdf",
                "attachment_id": "att-1",
                "size": 1024,
            }
        ],
    })
    assert compact["attachments"][0]["filename"] == "Invoice-2026.pdf"

    unsupported = json.dumps({
        "summaries": [
            {
                "message_id": "m1",
                "summary": "Please see the attached invoice.",
                "action": "None stated",
                "deadline": "Not specified",
            }
        ]
    })
    assert not gmail_agent_nodes._gmail_summary_is_grounded(
        unsupported,
        {"messages": [{**compact, "attachments": []}]},
    )


def test_rejected_summaries_do_not_produce_complete_metadata(monkeypatch):
    monkeypatch.setattr(gmail_agent_nodes, "emit_status", Mock())
    monkeypatch.setattr(gmail_agent_nodes, "emit_replace", Mock())
    messages = [
        _normalized("m1", "Routine operating update."),
        _normalized("m2", "Routine operating update."),
        _normalized("m3", "Routine operating update."),
    ]
    content = json.dumps({
        "summaries": [
            {
                "message_id": message["message_id"],
                "summary": "The company announced a 20% bonus.",
                "action": "None stated",
                "deadline": "Not specified",
            }
            for message in messages
        ]
    })
    monkeypatch.setattr(
        gmail_agent_nodes.llm_router,
        "generate",
        Mock(return_value=_llm_response(content)),
    )

    answer, _provider, _model, metadata = (
        gmail_agent_nodes._run_bounded_gmail_summary(
            state=_state(),
            result={
                "messages": messages,
                "requested_count": 3,
                "retrieved_count": 3,
                "failed_count": 0,
                "remaining_count": 0,
            },
            provider="mock",
            model="mock-model",
        )
    )

    assert metadata["status"] != "complete"
    assert metadata["processed_item_count"] == 0
    assert metadata["grounding_verified"] is False
    assert "**Emails summarized:** 0" in answer


def test_stream_error_never_echoes_raw_value_error():
    secret = "sk-secret-email-body-2026"
    assert secret not in chat_endpoint._stream_error_message(ValueError(secret))
    assert secret not in str(
        chat_endpoint._stream_http_exception(ValueError(secret)).detail
    )


def test_chat_stream_event_models_completion():
    event = ChatStreamEvent(type="done", completion={"status": "partial"})
    assert event.completion == {"status": "partial"}
    assert '"completion":{"status":"partial"}' in event.sse()
