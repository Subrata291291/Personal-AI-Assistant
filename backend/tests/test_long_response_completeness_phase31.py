"""Phase 31: bounded, grounded, complete long Gmail responses.

Every test is deterministic. GmailService, LLM providers, and the LangGraph
graph are replaced with in-memory fakes. No live Google API, live LLM provider,
credential, `.env`, OAuth scope, mutation, or deployment is involved.
"""

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
from langgraph.graph import END, START, StateGraph

from app.api.v1.endpoints import chat as chat_endpoint
from app.core.config import settings
from app.llm import router as router_module
from app.llm.fallback import FallbackManager
from app.llm.schemas import (
    LLMMessage,
    LLMRequest,
    LLMResponse,
    LLMStreamChunk,
)
from app.llm.streaming import emit_replace, generate_or_stream
from app.schemas.chat import ChatRequest
from app.services import chat_service as chat_service_module
from app.services.chat_service import ChatService
from app.workflows import gmail_agent_nodes, gmail_long_response, gmail_tools
from app.workflows.chat_state import ChatContext, ChatState


USER_ID = UUID("11111111-1111-1111-1111-111111111111")
OTHER_USER_ID = UUID("22222222-2222-2222-2222-222222222222")
CONVERSATION_ID = UUID("33333333-3333-3333-3333-333333333333")
REQUEST_TIME = "2026-11-20T10:00:00+05:30"
CONNECTION_ID = UUID("44444444-4444-4444-4444-444444444444")
USER_ID_STR = "user-phase31"


# ---------------------------------------------------------------------------
# Helpers / deterministic fakes
# ---------------------------------------------------------------------------


def _llm_response(
    content,
    *,
    finish_reason="stop",
    provider="mock",
    model="mock-model",
    usage=None,
):
    return LLMResponse(
        content=content,
        provider=provider,
        model=model,
        finish_reason=finish_reason,
        latency_ms=0,
        usage=usage,
    )


def _gmail_state(message="Summarize all my emails today.", **overrides):
    state = {
        "user_message": message,
        "user_id": USER_ID_STR,
        "conversation_id": "conversation-phase31",
        "assistant_response": "",
        "provider": "",
        "model": "",
        "messages": [],
        "user_timezone": "Asia/Kolkata",
        "gmail_connection_id": "conn-gmail-phase31",
        "request_time": REQUEST_TIME,
    }
    state.update(overrides)
    return state


def _message(message_id, *, index=0):
    """Authoritative detail record as returned by GmailService."""
    epoch_ms = int(
        datetime(2026, 11, 20, 4, 0, tzinfo=timezone.utc).timestamp() * 1000
    ) + index
    return {
        "id": message_id,
        "thread_id": f"thread-{message_id}",
        "internal_date": str(epoch_ms),
        "headers": {
            "from": f"sender{index}@example.com",
            "to": "user@example.com",
            "subject": f"Subject {message_id}",
            "date": "Fri, 20 Nov 2026 09:00:00 +0530",
        },
        "body": {"plain_text": f"report {message_id} is ready for review"},
        "labels": ["INBOX"],
    }


def _batch_response(batch, *, finish_reason="stop", omit=()):
    """A grounded JSON intermediate response for one authoritative batch."""
    summaries = []
    for message in batch:
        message_id = message["message_id"]
        if message_id in omit:
            continue
        summaries.append(
            {
                "message_id": message_id,
                "summary": f"report {message_id} is ready for review",
                "action": "None stated",
                "deadline": "Not specified",
            }
        )
    return _llm_response(
        json.dumps({"summaries": summaries}),
        finish_reason=finish_reason,
    )


def _responses_for(messages, **kwargs):
    normalized, _, _ = gmail_long_response.prepare_summary_messages(messages)
    return [
        _batch_response(batch, **kwargs)
        for batch in gmail_long_response.chunk_messages(normalized)
    ]


def _mock_generation(monkeypatch, responses):
    """Mock both blocking batches and legacy generation; return captured calls."""
    calls = []

    def fake_generate(request):
        calls.append(request)
        index = len(calls) - 1
        value = responses[index] if index < len(responses) else responses[-1]
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(gmail_agent_nodes.llm_router, "generate", fake_generate)
    monkeypatch.setattr(gmail_agent_nodes, "generate_or_stream", fake_generate)
    monkeypatch.setattr(gmail_agent_nodes, "emit_replace", Mock())
    monkeypatch.setattr(gmail_agent_nodes, "emit_status", Mock())
    return calls


def _run_summary(monkeypatch, messages, *, responses=None, result_extra=None, cancelled=False):
    responses = responses if responses is not None else _responses_for(messages)
    calls = _mock_generation(monkeypatch, responses)
    result = {"messages": messages, **{k: v for k, v in (result_extra or {}).items()}}
    output = gmail_agent_nodes.gmail_response_node(
        _gmail_state(),
        result=result,
        cancelled=cancelled,
    )
    return output, calls, result


class FakeGmailService:
    """In-memory GmailService: deterministic pages, details, and failures."""

    def __init__(self, pages=None, details=None, fail_ids=()):
        self.pages = list(pages or [])
        self.details = dict(details or {})
        self.fail_ids = set(fail_ids)
        self.list_calls = []
        self.detail_calls = []

    async def list_messages(self, **kwargs):
        self.list_calls.append(kwargs)
        if not self.pages:
            return {
                "messages": [],
                "result_size_estimate": 0,
                "next_page_token": None,
            }
        return self.pages.pop(0)

    async def get_messages_for_summary(self, *, message_ids, **_kwargs):
        self.detail_calls.append(list(message_ids))
        return [
            self.details[message_id]
            for message_id in message_ids
            if message_id in self.details and message_id not in self.fail_ids
        ]

    async def get_message(self, *, message_id, **_kwargs):
        if message_id in self.fail_ids or message_id not in self.details:
            raise RuntimeError("detail unavailable")
        return self.details[message_id]


class _GatewayError(RuntimeError):
    status_code = 500


class _FakeProvider:
    def __init__(self, name, chunks=(), error=None, fail_after_first=False):
        self.provider_name = name
        self.model = f"{name}-model"
        self.chunks = list(chunks)
        self.error = error
        self.fail_after_first = fail_after_first
        self.stream_calls = 0

    def stream(self, _request):
        self.stream_calls += 1
        for index, content in enumerate(self.chunks):
            yield LLMStreamChunk(
                content=content,
                provider=self.provider_name,
                model=self.model,
                is_final=index == len(self.chunks) - 1,
            )
            if self.fail_after_first:
                raise self.error
        if self.error is not None:
            raise self.error


class _FakeRegistry:
    def __init__(self, providers):
        self.providers = providers

    def is_configured(self, name):
        return name in self.providers

    def get_provider(self, name):
        return self.providers.get(name)


def _manager(providers, priority):
    manager = FallbackManager(registry=_FakeRegistry(providers))
    manager.provider_priority = priority
    return manager


# ChatService fakes ----------------------------------------------------------


class _FakeRequestRepository:
    def __init__(self):
        self.request_id = None
        self.status = "processing"
        self.statuses = []
        self.user_message_id = None
        self.assistant_message_id = None
        self.execution_started = False

    def _record(self):
        return {
            "id": self.request_id,
            "status": self.status,
            "user_message_id": self.user_message_id,
            "assistant_message_id": self.assistant_message_id,
            "execution_started_at": (
                datetime.now(timezone.utc) if self.execution_started else None
            ),
        }

    def create_request(self, **_kwargs):
        if self.request_id is None:
            self.request_id = uuid4()
        return self._record(), True

    def claim_request(self, **_kwargs):
        return {"id": self.request_id} if self.status == "processing" else None

    def attach_user_message(self, user_message_id=None, **_kwargs):
        self.user_message_id = user_message_id
        return {"attached": True}

    def mark_execution_started(self, **_kwargs):
        self.execution_started = True
        return {"started": True}

    def update_status(
        self,
        status=None,
        user_message_id=None,
        assistant_message_id=None,
        **_kwargs,
    ):
        self.status = status
        self.statuses.append(status)
        if user_message_id is not None:
            self.user_message_id = user_message_id
        if assistant_message_id is not None:
            self.assistant_message_id = assistant_message_id
        return {"updated": True}

    def get_request(self, **_kwargs):
        return self._record()

    def reconcile_completed_request(self, **_kwargs):
        return None


class _FakeMessageService:
    def __init__(self, owned=True):
        self.messages = []
        self.owned = owned
        self.ownership_checks = []

    def verify_conversation_ownership(self, *, user_id, conversation_id):
        self.ownership_checks.append((user_id, conversation_id))
        return self.owned

    def get_request_message(self, *, role, chat_request_id=None, **_kwargs):
        return next(
            (
                message
                for message in self.messages
                if message["role"] == role
                and (
                    chat_request_id is None
                    or message.get("chat_request_id") == chat_request_id
                )
            ),
            None,
        )

    def create_request_message(
        self,
        *,
        conversation_id,
        role,
        content,
        metadata=None,
        chat_request_id=None,
        **_kwargs,
    ):
        message = {
            "id": uuid4(),
            "conversation_id": conversation_id,
            "chat_request_id": chat_request_id,
            "role": role,
            "content": content,
            "metadata": metadata or {},
            "created_at": datetime.now(timezone.utc),
        }
        self.messages.append(message)
        return message

    def get_message(self, *, message_id, **_kwargs):
        return next(
            (message for message in self.messages if message["id"] == message_id),
            None,
        )

    def get_conversation_messages(self, **_kwargs):
        return list(self.messages)

    def update_request_message_content(self, *, message_id, content, **_kwargs):
        for message in self.messages:
            if message["id"] == message_id:
                message["content"] = content
                return message
        return None


def _make_service(owned=True):
    message_service = _FakeMessageService(owned=owned)
    request_repository = _FakeRequestRepository()
    service = ChatService(
        message_service=message_service,
        user_settings_service=SimpleNamespace(
            get_settings=lambda **_kwargs: SimpleNamespace(memory_enabled=False)
        ),
        memory_pipeline=SimpleNamespace(process=lambda **_kwargs: None),
        request_repository=request_repository,
        user_connection_service=None,
    )
    return service, message_service, request_repository


class _FakeGraph:
    def __init__(self, node):
        builder = StateGraph(ChatState, context_schema=ChatContext)
        builder.add_node("llm_node", node)
        builder.add_edge(START, "llm_node")
        builder.add_edge("llm_node", END)
        self.app = builder.compile()
        self.invoke_calls = 0
        self.stream_calls = 0

    def stream(self, state, config=None, context=None, stream_mode=None):
        self.stream_calls += 1
        return self.app.stream(
            state,
            config=config,
            context=context,
            stream_mode=stream_mode,
        )

    def invoke(self, state, config=None, context=None):
        self.invoke_calls += 1
        return self.app.invoke(state, config=config, context=context)


def _deterministic_node(state):
    answer = "Here is your capability summary."
    return {
        "assistant_response": answer,
        "provider": "",
        "model": "",
        "messages": [
            {"role": "user", "content": state["user_message"]},
            {"role": "assistant", "content": answer},
        ],
    }


class _FakeEndpointService:
    def __init__(self, events):
        self.events = events
        self.calls = []

    def stream_message(self, **kwargs):
        self.calls.append(kwargs)
        yield from self.events


def _parse_sse(chunks):
    if hasattr(chunks, "__aiter__"):
        async def collect():
            items = []
            async for chunk in chunks:
                items.append(chunk)
            return items

        chunks = asyncio.run(collect())

    events = []
    for chunk in chunks:
        text = chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
        for line in text.splitlines():
            if line.startswith("data:"):
                events.append(json.loads(line[5:].strip()))
    return events


# ---------------------------------------------------------------------------
# 1-3. Response size matrix
# ---------------------------------------------------------------------------


def test_01_short_response_uses_single_legacy_call_without_batching(monkeypatch):
    calls = _mock_generation(
        monkeypatch,
        [_llm_response("I found one matching email.")],
    )
    output = gmail_agent_nodes.gmail_response_node(
        _gmail_state("Show unread emails"),
        result={
            "messages": [_message("m1")],
            "search_criteria": "is:unread",
            "requested_count": 1,
            "retrieved_count": 1,
        },
    )

    assert len(calls) == 1
    assert calls[0].max_tokens == 1800
    assert output["assistant_response"] == "I found one matching email."
    assert output["response_metadata"]["status"] == "complete"


def test_02_ten_emails_are_summarized_in_two_bounded_batches(monkeypatch):
    messages = [_message(f"m{i}", index=i) for i in range(10)]
    output, calls, _ = _run_summary(
        monkeypatch,
        messages,
        result_extra={"requested_count": 10, "retrieved_count": 10},
    )

    assert len(calls) == 2
    assert output["assistant_response"].count("#### ") == 10
    assert output["response_metadata"]["gmail_batch_count"] == 2
    assert output["response_metadata"]["status"] == "complete"


def test_03_thirty_emails_are_processed_in_six_bounded_batches(monkeypatch):
    messages = [_message(f"m{i}", index=i) for i in range(30)]
    output, calls, _ = _run_summary(
        monkeypatch,
        messages,
        result_extra={"requested_count": 30, "retrieved_count": 30},
    )

    assert len(calls) == 6
    assert output["assistant_response"].count("#### ") == 30
    for call in calls:
        assert call.max_tokens == gmail_long_response.SUMMARY_BATCH_MAX_TOKENS
        assert call.max_tokens < 1800
        assert call.messages[-1].content.count('"message_id"') <= 5
    assert output["response_metadata"]["status"] == "complete"


# ---------------------------------------------------------------------------
# 4-7. Retrieval: pagination, dedupe, partial and total failure
# ---------------------------------------------------------------------------


def test_04_pagination_follows_tokens_and_flags_remaining():
    class PagingService:
        def __init__(self):
            self.calls = []

        async def list_messages(self, **kwargs):
            self.calls.append(kwargs)
            token = kwargs.get("page_token")
            if token is None:
                return {
                    "messages": [{"id": "a"}, {"id": "b"}],
                    "next_page_token": "t2",
                    "result_size_estimate": 5,
                }
            if token == "t2":
                return {
                    "messages": [{"id": "b"}, {"id": "c"}],
                    "next_page_token": "t3",
                    "result_size_estimate": 5,
                }
            return {
                "messages": [{"id": "d"}],
                "next_page_token": None,
                "result_size_estimate": 5,
            }

    service = PagingService()
    collected, estimate, has_more = asyncio.run(
        gmail_tools._collect_message_pages_bounded(
            service=service,
            user_id=CONNECTION_ID,
            connection_id=CONNECTION_ID,
            query=None,
            folder="all",
            page_size=2,
            max_pages=5,
            target_count=10,
        )
    )

    assert [item["id"] for item in collected] == ["a", "b", "c", "d"]
    assert estimate == 5
    assert has_more is True
    assert [call["page_token"] for call in service.calls] == [None, "t2", "t3"]


def test_05_duplicate_message_ids_are_deduplicated_before_rendering(monkeypatch):
    messages = [_message("m1"), _message("m1"), _message("m2")]
    output, calls, _ = _run_summary(
        monkeypatch,
        messages,
        result_extra={"requested_count": 2, "retrieved_count": 2},
    )

    unique, duplicates, invalid = gmail_long_response.prepare_summary_messages(
        messages
    )
    assert len(unique) == 2
    assert duplicates == 1
    assert invalid == 0
    assert len(calls) == 1
    assert output["assistant_response"].count("#### ") == 2
    assert output["response_metadata"]["duplicate_item_count"] == 1

    # Deduplication also happens at the retrieval layer.
    deduped, _, _ = asyncio.run(
        gmail_tools._collect_message_pages_bounded(
            service=FakeGmailService(
                pages=[
                    {
                        "messages": [{"id": "m1"}, {"id": "m1"}],
                        "next_page_token": None,
                        "result_size_estimate": 2,
                    }
                ]
            ),
            user_id=CONNECTION_ID,
            connection_id=CONNECTION_ID,
            query=None,
            folder="all",
            target_count=2,
        )
    )
    assert [item["id"] for item in deduped] == ["m1"]


def test_06_one_retrieval_failure_is_partial_and_never_fabricated(monkeypatch):
    details = {f"m{i}": _message(f"m{i}", index=i) for i in range(1, 6)}
    service = FakeGmailService(
        pages=[
            {
                "messages": [{"id": key} for key in details],
                "next_page_token": None,
                "result_size_estimate": 5,
            }
        ],
        details=details,
        fail_ids={"m5"},
    )
    retrieval = asyncio.run(
        gmail_tools._execute_with_service(
            service=service,
            intent="summarize_unread",
            plan={"intent": "summarize_unread", "max_results": 5},
            user_id=CONNECTION_ID,
            connection_id=CONNECTION_ID,
        )
    )

    assert retrieval["retrieved_count"] == 4
    assert retrieval["failed_count"] == 1
    assert retrieval["remaining_count"] >= 1

    output, _, _ = _run_summary(
        monkeypatch,
        retrieval["messages"],
        result_extra={
            k: retrieval[k]
            for k in (
                "requested_count",
                "retrieved_count",
                "failed_count",
                "failed_message_ids",
                "remaining_count",
            )
        },
    )
    metadata = output["response_metadata"]
    assert metadata["status"] == "partial"
    assert metadata["failed_item_count"] == 1
    assert "m5" not in output["assistant_response"]
    assert "Subject m5" not in output["assistant_response"]


def test_07_all_retrieval_failures_produce_failed_status_not_no_mail(monkeypatch):
    output = gmail_agent_nodes.gmail_response_node(
        _gmail_state("Summarize all my emails today."),
        result={
            "messages": [],
            "requested_count": 3,
            "failed_count": 3,
            "failed_message_ids": ["m1", "m2", "m3"],
            "remaining_count": 3,
            "period": "today (2026-11-20, Asia/Kolkata)",
        },
    )

    metadata = output["response_metadata"]
    assert metadata["status"] == "failed"
    assert metadata["failed_item_count"] == 3
    assert metadata["processed_item_count"] == 0
    assert "couldn't retrieve" in output["assistant_response"]
    assert "no incoming email" not in output["assistant_response"]


# ---------------------------------------------------------------------------
# 8-10. Output-limit handling
# ---------------------------------------------------------------------------


def test_08_batch_output_limit_uses_deterministic_fallback(monkeypatch):
    messages = [_message(f"m{i}", index=i) for i in range(10)]
    responses = _responses_for(messages)
    responses[1] = _batch_response(
        gmail_long_response.chunk_messages(
            gmail_long_response.prepare_summary_messages(messages)[0]
        )[1],
        finish_reason="length",
    )
    output, calls, _ = _run_summary(
        monkeypatch,
        messages,
        responses=responses,
        result_extra={"requested_count": 10, "retrieved_count": 10},
    )

    assert len(calls) == 2
    assert output["response_metadata"]["output_limited"] is True
    assert output["response_metadata"]["status"] == "partial"
    assert "A bounded batch reached its output limit" in output["assistant_response"]
    # Every retrieved message still gets a card with authoritative fallback text.
    assert output["assistant_response"].count("#### ") == 10
    assert "Retrieved content:" in output["assistant_response"]


def test_09_aggregation_output_limit_cannot_truncate_whole_answer(monkeypatch):
    messages = [_message(f"m{i}", index=i) for i in range(30)]
    responses = _responses_for(messages)
    responses[-1] = _batch_response(
        gmail_long_response.chunk_messages(
            gmail_long_response.prepare_summary_messages(messages)[0]
        )[-1],
        finish_reason="max_tokens",
    )
    output, calls, _ = _run_summary(
        monkeypatch,
        messages,
        responses=responses,
        result_extra={"requested_count": 30, "retrieved_count": 30},
    )

    # No extra "final aggregation" LLM call: aggregation is deterministic.
    assert len(calls) == 6
    assert output["assistant_response"].count("#### ") == 30
    assert output["response_metadata"]["status"] == "partial"
    assert output["response_metadata"]["output_limited"] is True


def test_10_length_finish_reason_maps_to_explicit_output_limited():
    limited = _llm_response("cut off", finish_reason="length")
    assert limited.output_limited is True
    assert limited.completion_metadata()["status"] == "truncated"
    assert gmail_agent_nodes._response_output_limited(limited) is True
    assert gmail_agent_nodes._response_output_limited(
        SimpleNamespace(finish_reason="max_tokens")
    ) is True

    complete = _llm_response("done", finish_reason="stop")
    assert complete.output_limited is False
    assert complete.completion_metadata()["status"] == "complete"
    assert gmail_agent_nodes._response_output_limited(complete) is False


# ---------------------------------------------------------------------------
# 11-13. Provider failure / fallback boundary
# ---------------------------------------------------------------------------


def test_11_network_failure_in_one_batch_is_partial_with_fallback(monkeypatch):
    messages = [_message(f"m{i}", index=i) for i in range(10)]
    responses = _responses_for(messages)
    responses[0] = RuntimeError("simulated network failure")
    output, calls, _ = _run_summary(monkeypatch, messages, responses=responses)

    assert len(calls) == 2
    assert output["response_metadata"]["status"] == "partial"
    assert output["assistant_response"].count("#### ") == 10
    assert "Retrieved content:" in output["assistant_response"]


def test_12_fallback_before_first_token_switches_provider():
    failing = _FakeProvider("groq", error=_GatewayError("boom"))
    healthy = _FakeProvider("openrouter", chunks=["ok", "!"])
    manager = _manager(
        {"groq": failing, "openrouter": healthy},
        ["groq", "openrouter"],
    )

    chunks = list(
        manager.stream(
            LLMRequest(messages=[LLMMessage(role="user", content="hi")])
        )
    )

    assert "".join(chunk.content for chunk in chunks) == "ok!"
    assert failing.stream_calls == 1
    assert healthy.stream_calls == 1


def test_13_fallback_after_first_token_never_appends_another_provider():
    partial = _FakeProvider(
        "groq",
        chunks=["partial"],
        error=_GatewayError("boom"),
        fail_after_first=True,
    )
    healthy = _FakeProvider("openrouter", chunks=["should-not-appear"])
    manager = _manager(
        {"groq": partial, "openrouter": healthy},
        ["groq", "openrouter"],
    )

    received = []
    with pytest.raises(_GatewayError):
        for chunk in manager.stream(
            LLMRequest(messages=[LLMMessage(role="user", content="hi")])
        ):
            received.append(chunk)

    assert "".join(chunk.content for chunk in received) == "partial"
    assert healthy.stream_calls == 0


# ---------------------------------------------------------------------------
# 14-19. Streaming, persistence, grounding, counts, completeness
# ---------------------------------------------------------------------------


def test_14_multi_batch_emits_explicit_progress_status(monkeypatch):
    messages = [_message(f"m{i}", index=i) for i in range(12)]
    status = Mock()
    monkeypatch.setattr(gmail_agent_nodes, "emit_status", status)
    monkeypatch.setattr(gmail_agent_nodes, "emit_replace", Mock())
    monkeypatch.setattr(
        gmail_agent_nodes.llm_router,
        "generate",
        Mock(side_effect=_responses_for(messages)),
    )

    gmail_agent_nodes.gmail_response_node(
        _gmail_state(),
        result={
            "messages": messages,
            "requested_count": 12,
            "retrieved_count": 12,
        },
    )

    progress = [call.args[0] for call in status.call_args_list]
    assert "Processing email batch 1 of 3..." in progress
    assert "Processing email batch 3 of 3..." in progress
    assert "Preparing final summary..." in progress
    assert "Verifying response completeness..." in progress


def test_15_one_assistant_message_is_persisted_per_request(monkeypatch):
    graph = _FakeGraph(_deterministic_node)
    monkeypatch.setattr(chat_service_module, "chat_graph", graph)
    service, messages, repository = _make_service()

    response = service.send_message(
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        content="What can you do?",
        idempotency_key=uuid4(),
        user_timezone="UTC",
    )

    assert [item["role"] for item in messages.messages] == ["user", "assistant"]
    assert repository.statuses.count("completed") == 1
    assert response["assistant_message"]["content"] == (
        "Here is your capability summary."
    )


def test_16_no_duplicate_or_fabricated_email_cards(monkeypatch):
    messages = [_message("m1"), _message("m2"), _message("m3")]
    output, _, _ = _run_summary(
        monkeypatch,
        messages + [_message("m2")],
        result_extra={"requested_count": 3, "retrieved_count": 3},
    )

    assert output["assistant_response"].count("#### ") == 3
    assert "sender99@example.com" not in output["assistant_response"]
    assert "mallory@evil.example" not in output["assistant_response"]
    assert output["response_metadata"]["gmail_grounding_verified"] is True
    # The source reference is retained, not withheld from the final record.
    assert output.get("gmail_sources") is None  # result had no sources


def test_17_truncated_body_never_claims_all_emails_were_reviewed(monkeypatch):
    messages = [_message(f"m{i}", index=i) for i in range(5)]
    captured = {}
    monkeypatch.setattr(
        gmail_agent_nodes,
        "llm_router",
        SimpleNamespace(
            generate=lambda request: (
                captured.setdefault("prompt", request.messages[-1].content)
                or _batch_response(
                    gmail_long_response.chunk_messages(messages)[0]
                )
            )
        ),
    )
    monkeypatch.setattr(gmail_agent_nodes, "emit_replace", Mock())
    monkeypatch.setattr(gmail_agent_nodes, "emit_status", Mock())

    output = gmail_agent_nodes.gmail_response_node(
        _gmail_state("Summarize all my emails today."),
        result={
            "messages": messages,
            "content_truncated": True,
            "requested_count": 5,
            "retrieved_count": 5,
            "remaining_count": 4,
        },
    )

    assert "supplied data was truncated" in captured["prompt"]
    assert "do not claim that no other messages" in captured["prompt"]
    assert "**Coverage note:**" in output["assistant_response"]
    assert output["response_metadata"]["status"] == "partial"
    assert "all emails" not in output["assistant_response"].casefold()


def test_18_counts_are_accurate_and_rendered_from_retrieved_data(monkeypatch):
    details = [
        gmail_tools._compact_message(_message(f"m{i}", index=i))
        for i in range(1, 4)
    ]
    annotated = gmail_tools._annotate_retrieval(
        {
            "messages": details,
            "content_truncated": False,
        },
        requested_count=6,
        candidate_count=5,
        estimates=6,
        details=details,
        failed_ids=["m4"],
        beyond_limit=0,
        has_more=False,
    )

    assert annotated["requested_count"] == 6
    assert annotated["candidate_count"] == 5
    assert annotated["retrieved_count"] == 3
    assert annotated["processed_count"] == 3
    assert annotated["failed_count"] == 1
    assert annotated["remaining_count"] == 3
    assert annotated["retrieved_message_ids"] == ["m1", "m2", "m3"]

    output, _, _ = _run_summary(
        monkeypatch,
        details,
        result_extra={
            "requested_count": annotated["requested_count"],
            "retrieved_count": annotated["retrieved_count"],
            "failed_count": annotated["failed_count"],
            "remaining_count": annotated["remaining_count"],
        },
    )
    metadata = output["response_metadata"]
    assert metadata["requested_item_count"] == 6
    assert metadata["retrieved_item_count"] == 3
    assert metadata["processed_item_count"] == 3
    assert metadata["failed_item_count"] == 1
    assert metadata["remaining_item_count"] == 3
    assert "**Emails requested:** 6" in output["assistant_response"]
    assert "**Remaining:** 3" in output["assistant_response"]


def test_19_no_false_completeness_when_results_remain(monkeypatch):
    messages = [_message(f"m{i}", index=i) for i in range(5)]
    output, _, _ = _run_summary(
        monkeypatch,
        messages,
        result_extra={
            "requested_count": 5,
            "retrieved_count": 5,
            "remaining_count": 12,
            "has_more": True,
        },
    )

    assert output["response_metadata"]["status"] == "partial"
    assert "**Completion status:** partial." in output["assistant_response"]
    assert "did not include every matching message" in output["assistant_response"]


# ---------------------------------------------------------------------------
# 20-21. SSE transport contract
# ---------------------------------------------------------------------------


def test_20_sse_done_always_carries_completion_metadata():
    completion = {
        "status": "partial",
        "requested_item_count": 30,
        "processed_item_count": 30,
        "remaining_item_count": 5,
    }
    payload = {
        "conversation_id": CONVERSATION_ID,
        "user_message": {"id": uuid4(), "content": "hi"},
        "assistant_message": {
            "id": uuid4(),
            "content": "bounded answer",
            "metadata": {"response_completion": completion},
        },
        "provider": "groq",
        "model": "test-model",
    }
    service = _FakeEndpointService(
        [
            {"type": "token", "content": "bounded answer"},
            {"type": "done", "response": payload},
        ]
    )

    response = chat_endpoint.chat_stream(
        payload=ChatRequest(conversation_id=CONVERSATION_ID, content="hi"),
        user={"id": str(USER_ID), "user_metadata": {}},
        service=service,
        idempotency_key=uuid4(),
    )
    events = _parse_sse(response.body_iterator)
    done = [event for event in events if event["type"] == "done"]

    assert len(done) == 1
    assert done[0]["completion"] == completion
    assert "assistant_message" not in done[0]


def test_21_replace_event_overwrites_streamed_partial_content(monkeypatch):
    def fake_stream(_request):
        yield LLMStreamChunk(
            content="partial ",
            provider="groq",
            model="test-model",
            is_final=True,
        )

    monkeypatch.setattr(router_module.llm_router, "stream", fake_stream)

    def node(state):
        response = generate_or_stream(
            LLMRequest(messages=[LLMMessage(role="user", content="hi")])
        )
        answer = "verified final answer"
        emit_replace(answer)
        return {
            "assistant_response": answer,
            "provider": response.provider,
            "model": response.model,
            "messages": [
                {"role": "user", "content": state["user_message"]},
                {"role": "assistant", "content": answer},
            ],
        }

    graph = _FakeGraph(node)
    monkeypatch.setattr(chat_service_module, "chat_graph", graph)
    service, messages, _repository = _make_service()

    events = list(
        service.stream_message(
            user_id=USER_ID,
            conversation_id=CONVERSATION_ID,
            content="hi",
            idempotency_key=uuid4(),
            user_timezone="UTC",
        )
    )
    tokens = [event["content"] for event in events if event.get("type") == "token"]
    replaces = [event["content"] for event in events if event.get("type") == "replace"]

    assert tokens == ["partial "]
    assert replaces and all(text == "verified final answer" for text in replaces)
    assert events[-1]["response"]["assistant_message"]["content"] == (
        "verified final answer"
    )
    assert [item["role"] for item in messages.messages] == ["user", "assistant"]


# ---------------------------------------------------------------------------
# 22-24. Cancellation, replay, tenant isolation
# ---------------------------------------------------------------------------


def test_22_cancellation_marks_status_cancelled_without_processing(monkeypatch):
    messages = [_message(f"m{i}", index=i) for i in range(5)]
    output, calls, _ = _run_summary(
        monkeypatch,
        messages,
        responses=_responses_for(messages),
        cancelled=True,
    )

    assert calls == []
    assert "cancelled" in output["assistant_response"].casefold()
    metadata = output["response_metadata"]
    assert metadata["status"] == "cancelled"
    assert metadata["processed_item_count"] == 0
    assert metadata["gmail_grounding_verified"] is False


def test_23_idempotent_replay_returns_one_assistant_message(monkeypatch):
    graph = _FakeGraph(_deterministic_node)
    monkeypatch.setattr(chat_service_module, "chat_graph", graph)
    service, messages, repository = _make_service()
    key = uuid4()

    first = service.send_message(
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        content="What can you do?",
        idempotency_key=key,
        user_timezone="UTC",
    )
    second = service.send_message(
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        content="What can you do?",
        idempotency_key=key,
        user_timezone="UTC",
    )

    assert first["assistant_message"]["id"] == second["assistant_message"]["id"]
    assert graph.invoke_calls == 1
    assert [item["role"] for item in messages.messages] == ["user", "assistant"]
    assert repository.statuses.count("completed") == 1


def test_24_tenant_isolation_is_enforced_before_streaming():
    service, messages, repository = _make_service(owned=False)
    stream = service.stream_message(
        user_id=OTHER_USER_ID,
        conversation_id=CONVERSATION_ID,
        content="Summarize all my emails today.",
        idempotency_key=uuid4(),
        user_timezone="UTC",
    )

    with pytest.raises(LookupError):
        next(stream)

    assert messages.ownership_checks == [(OTHER_USER_ID, CONVERSATION_ID)]
    assert repository.statuses == []
    assert messages.messages == []


# ---------------------------------------------------------------------------
# 25-30. Routing, parity, priority, no false warnings
# ---------------------------------------------------------------------------


def test_25_gmail_all_today_plan_is_bounded_and_clarification_is_preserved(
    monkeypatch,
):
    monkeypatch.setattr(
        gmail_agent_nodes.llm_router,
        "generate",
        Mock(return_value=_llm_response(json.dumps({"intent": "general"}))),
    )
    routed = gmail_agent_nodes.gmail_intent_router_node(
        _gmail_state("Check my all today's mail and summarize them step by step.")
    )
    plan = routed.get("gmail_plan") or {}

    assert routed["assistant_route"] == "gmail"
    assert plan["intent"] == "summarize_today"
    assert plan["max_results"] == gmail_long_response.LONG_SUMMARY_MIN_MESSAGES + 47

    clarified = gmail_agent_nodes.gmail_response_node(
        _gmail_state("Which one?", gmail_plan={"intent": "clarify"}),
        direct_response="Which email did you mean?",
    )
    assert clarified["assistant_response"] == "Which email did you mean?"


def test_26_calendar_and_contacts_routes_are_not_hijacked_by_gmail(monkeypatch):
    monkeypatch.setattr(
        gmail_agent_nodes.llm_router,
        "generate",
        Mock(return_value=_llm_response(json.dumps({"intent": "general"}))),
    )

    calendar = gmail_agent_nodes.gmail_intent_router_node(
        _gmail_state("Show my calendar events today.")
    )
    contacts = gmail_agent_nodes.gmail_intent_router_node(
        _gmail_state("Show my contacts.")
    )

    assert calendar["assistant_route"] == "google_calendar"
    assert contacts["assistant_route"] == "google_contacts"


def test_27_streaming_and_non_streaming_return_identical_content(monkeypatch):
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

    blocking = service.send_message(
        user_id=uuid4(),
        conversation_id=uuid4(),
        content="Show my emails",
        idempotency_key=uuid4(),
    )
    streaming = list(
        service.stream_message(
            user_id=uuid4(),
            conversation_id=uuid4(),
            content="Show my emails",
            idempotency_key=uuid4(),
        )
    )

    assert blocking["assistant_message"]["content"] == "grounded answer"
    assert [event["type"] for event in streaming] == ["token", "done"]
    assert [call["streaming"] for call in captured] == [False, True]


def test_28_deterministic_route_emits_one_final_content_event(monkeypatch):
    graph = _FakeGraph(_deterministic_node)
    monkeypatch.setattr(chat_service_module, "chat_graph", graph)
    service, _messages, _repository = _make_service()

    events = list(
        service.stream_message(
            user_id=USER_ID,
            conversation_id=CONVERSATION_ID,
            content="What can you do?",
            idempotency_key=uuid4(),
            user_timezone="UTC",
        )
    )
    tokens = [event["content"] for event in events if event.get("type") == "token"]

    assert tokens == ["Here is your capability summary."]
    assert events[-1]["type"] == "done"
    assert events[-1]["response"]["assistant_message"]["content"] == tokens[0]


def test_29_complete_response_does_not_show_a_false_warning(monkeypatch):
    messages = [_message(f"m{i}", index=i) for i in range(5)]
    output, _, _ = _run_summary(
        monkeypatch,
        messages,
        result_extra={
            "requested_count": 5,
            "retrieved_count": 5,
            "remaining_count": 0,
        },
    )

    metadata = output["response_metadata"]
    assert metadata["status"] == "complete"
    assert metadata["output_limited"] is False
    assert metadata["grounding_verified"] is True
    assert "Completion status" not in output["assistant_response"]
    assert "Coverage note" not in output["assistant_response"]
    assert "reached its output limit" not in output["assistant_response"]


def test_30_provider_priority_and_batch_limits_are_preserved():
    priority = [
        name.casefold() for name in settings.llm_provider_priority_list
    ]
    assert priority == ["groq", "openrouter", "gemini", "openai"]
    assert gmail_long_response.SUMMARY_BATCH_MAX_TOKENS <= 2048
    assert gmail_long_response.SUMMARY_BATCH_MAX_TOKENS < 1800
    assert gmail_long_response.SUMMARY_BATCH_SIZE == 5
    assert set(gmail_long_response.COMPLETENESS_STATUSES) == {
        "complete",
        "partial",
        "failed",
        "cancelled",
    }
