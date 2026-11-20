"""Phase 11 regressions: genuine end-to-end token streaming.

The key proof is ``test_first_token_arrives_before_generation_completes``:
a deliberately delayed fake LLM blocks after its first chunk, and the test
asserts that the service surfaced that chunk before the mock was allowed to
finish generating.
"""

import asyncio
import contextvars
import json
import threading
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from langgraph.graph import END, START, StateGraph
from starlette.concurrency import iterate_in_threadpool

from app.api.v1.endpoints import chat as chat_endpoint
from app.llm import router as router_module
from app.llm.fallback import FallbackManager
from app.llm.schemas import (
    LLMMessage,
    LLMRequest,
    LLMResponse,
    LLMStreamChunk,
)
from app.llm.streaming import (
    generate_or_stream,
    streaming_enabled,
    streaming_mode,
)
from app.schemas.chat import ChatRequest
from app.services import chat_service as chat_service_module
from app.services.chat_service import ChatRequestFailedError, ChatService
from app.workflows.chat_state import ChatContext, ChatState


USER_ID = UUID("11111111-1111-1111-1111-111111111111")
OTHER_USER_ID = UUID("22222222-2222-2222-2222-222222222222")
CONVERSATION_ID = UUID("33333333-3333-3333-3333-333333333333")


class FakeRequestRepository:
    def __init__(self):
        self.request_id = None
        self.status = "processing"
        self.user_message_id = None
        self.assistant_message_id = None
        self.execution_started = False
        self.statuses = []

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
        if self.status != "processing":
            return None
        return {"id": self.request_id}

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


class FakeMessageService:
    def __init__(self, owned=True):
        self.messages = []
        self.owned = owned
        self.ownership_checks = []

    def verify_conversation_ownership(self, *, user_id, conversation_id):
        self.ownership_checks.append((user_id, conversation_id))
        return self.owned

    def get_request_message(
        self,
        *,
        role,
        chat_request_id=None,
        **_kwargs,
    ):
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
            (
                message
                for message in self.messages
                if message["id"] == message_id
            ),
            None,
        )

    def get_conversation_messages(self, **_kwargs):
        return list(self.messages)

    def update_request_message_content(
        self,
        *,
        message_id,
        content,
        **_kwargs,
    ):
        for message in self.messages:
            if message["id"] == message_id:
                message["content"] = content
                return message
        return None


def _make_service(owned=True):
    message_service = FakeMessageService(owned=owned)
    request_repository = FakeRequestRepository()
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


class FakeStreamingGraph:
    """Minimal compiled LangGraph whose node uses the real streaming bridge."""

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
        return self.app.invoke(
            state,
            config=config,
            context=context,
        )


def _llm_node(state):
    response = generate_or_stream(
        LLMRequest(
            messages=[LLMMessage(role="user", content=state["user_message"])]
        )
    )
    return {
        "assistant_response": response.content,
        "provider": response.provider,
        "model": response.model,
        "messages": [
            {"role": "user", "content": state["user_message"]},
            {"role": "assistant", "content": response.content},
        ],
    }


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


class DelayedStream:
    """Fake LLM stream that blocks after the first chunk until released."""

    def __init__(self):
        self.first_chunk_emitted = threading.Event()
        self.release = threading.Event()
        self.completed = threading.Event()

    def stream(self, request):
        def generator():
            yield LLMStreamChunk(
                content="Hel",
                provider="groq",
                model="test-model",
            )
            self.first_chunk_emitted.set()
            if not self.release.wait(timeout=10):
                raise RuntimeError("test did not release the delayed stream")
            yield LLMStreamChunk(
                content="lo",
                provider="groq",
                model="test-model",
            )
            yield LLMStreamChunk(
                content="!",
                provider="groq",
                model="test-model",
                is_final=True,
            )
            self.completed.set()

        return generator()


def _collect_stream(service, **overrides):
    kwargs = {
        "user_id": USER_ID,
        "conversation_id": CONVERSATION_ID,
        "content": "Say hello",
        "idempotency_key": uuid4(),
        "user_timezone": "UTC",
    }
    kwargs.update(overrides)
    return list(service.stream_message(**kwargs))


def test_first_token_arrives_before_generation_completes(monkeypatch):
    delayed = DelayedStream()
    monkeypatch.setattr(router_module.llm_router, "stream", delayed.stream)

    graph = FakeStreamingGraph(_llm_node)
    monkeypatch.setattr(chat_service_module, "chat_graph", graph)

    service, _message_service, _repository = _make_service()
    events = []
    first_token_seen = threading.Event()
    consumer_finished = threading.Event()

    def consume():
        try:
            for event in service.stream_message(
                user_id=USER_ID,
                conversation_id=CONVERSATION_ID,
                content="Say hello",
                idempotency_key=uuid4(),
                user_timezone="UTC",
            ):
                events.append(event)
                if event.get("type") == "token":
                    first_token_seen.set()
        finally:
            consumer_finished.set()

    thread = threading.Thread(target=consume, daemon=True)
    thread.start()

    try:
        assert first_token_seen.wait(timeout=10), (
            "no token was surfaced before the delayed LLM finished"
        )
        # The mock is still blocked at release.wait(), so the fact that a token
        # was already observed proves progressive delivery, not post-hoc slicing.
        assert delayed.first_chunk_emitted.is_set()
        assert not delayed.completed.is_set()
        assert [e["content"] for e in events if e.get("type") == "token"] == [
            "Hel"
        ]
    finally:
        delayed.release.set()
        thread.join(timeout=10)

    assert consumer_finished.is_set()
    assert not thread.is_alive()

    tokens = [e["content"] for e in events if e.get("type") == "token"]
    assert tokens == ["Hel", "lo", "!"]
    assert "".join(tokens) == "Hello!"

    done_events = [e for e in events if e.get("type") == "done"]
    assert len(done_events) == 1
    persisted = done_events[0]["response"]["assistant_message"]["content"]
    assert persisted == "".join(tokens)
    assert graph.stream_calls == 1
    assert graph.invoke_calls == 0


def test_streaming_mode_sets_and_resets_in_one_context():
    assert streaming_enabled() is False
    with streaming_mode():
        assert streaming_enabled() is True
    assert streaming_enabled() is False


def test_streaming_mode_resets_after_exception():
    with pytest.raises(RuntimeError, match="boom"):
        with streaming_mode():
            assert streaming_enabled() is True
            raise RuntimeError("boom")

    assert streaming_enabled() is False


def test_streaming_mode_is_scoped_to_each_cross_context_advance(monkeypatch):
    calls = {"stream": 0, "generate": 0}

    def fake_stream(request):
        calls["stream"] += 1
        return iter([
            LLMStreamChunk(
                content="Hel",
                provider="groq",
                model="test-model",
            ),
            LLMStreamChunk(
                content="lo",
                provider="groq",
                model="test-model",
                is_final=True,
            ),
        ])

    def fake_generate(request):
        calls["generate"] += 1
        return LLMResponse(
            content="blocking",
            provider="groq",
            model="test-model",
            latency_ms=1,
        )

    monkeypatch.setattr(router_module.llm_router, "stream", fake_stream)
    monkeypatch.setattr(router_module.llm_router, "generate", fake_generate)
    graph = FakeStreamingGraph(_llm_node)
    monkeypatch.setattr(chat_service_module, "chat_graph", graph)
    service, _messages, repository = _make_service()

    stream = service.stream_message(
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        content="Say hello",
        idempotency_key=uuid4(),
        user_timezone="UTC",
    )

    # The endpoint pulls the first event in one context. Starlette then
    # advances the rest of the generator through iterate_in_threadpool, whose
    # worker contexts do not contain the first context's ContextVar token.
    first_context = contextvars.Context()
    first_event = first_context.run(next, stream)

    async def consume_rest():
        return [event async for event in iterate_in_threadpool(stream)]

    rest = asyncio.run(consume_rest())
    events = [first_event, *rest]

    tokens = [
        event["content"]
        for event in events
        if event.get("type") == "token"
    ]
    assert tokens == ["Hel", "lo"]
    assert calls["stream"] == 1
    assert calls["generate"] == 0
    assert events[-1]["type"] == "done"
    assert repository.statuses.count("completed") == 1
    assert streaming_enabled() is False


def test_streaming_context_is_cleared_when_closed_from_another_context(
    monkeypatch,
):
    def fake_stream(request):
        return iter([
            LLMStreamChunk(
                content="Hel",
                provider="groq",
                model="test-model",
            ),
            LLMStreamChunk(
                content="lo",
                provider="groq",
                model="test-model",
                is_final=True,
            ),
        ])

    monkeypatch.setattr(router_module.llm_router, "stream", fake_stream)
    graph = FakeStreamingGraph(_llm_node)
    monkeypatch.setattr(chat_service_module, "chat_graph", graph)
    service, messages, repository = _make_service()

    stream = service.stream_message(
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        content="Say hello",
        idempotency_key=uuid4(),
        user_timezone="UTC",
    )

    first = next(stream)
    assert first["type"] == "status"
    second = next(stream)
    assert second["type"] == "token"
    assert second["content"] == "Hel"

    # Client disconnect: the endpoint closes the generator from a different
    # execution context than the one that advanced it.
    contextvars.Context().run(stream.close)

    assert streaming_enabled() is False
    assert "completed" not in repository.statuses
    # A disconnect before persistence is terminal, even when close() is
    # invoked from another execution context.
    assert "failed" in repository.statuses
    assert [message["role"] for message in messages.messages] == ["user"]


def test_concurrent_async_streams_do_not_interfere(monkeypatch):
    def fake_stream(request):
        prefix = request.messages[-1].content
        return iter([
            LLMStreamChunk(
                content=f"{prefix}:1",
                provider="groq",
                model="test-model",
            ),
            LLMStreamChunk(
                content=f"{prefix}:2",
                provider="groq",
                model="test-model",
                is_final=True,
            ),
        ])

    monkeypatch.setattr(router_module.llm_router, "stream", fake_stream)
    graph = FakeStreamingGraph(_llm_node)
    monkeypatch.setattr(chat_service_module, "chat_graph", graph)

    async def consume(content):
        service, _messages, _repository = _make_service()
        stream = service.stream_message(
            user_id=USER_ID,
            conversation_id=CONVERSATION_ID,
            content=content,
            idempotency_key=uuid4(),
            user_timezone="UTC",
        )
        return [
            event
            async for event in iterate_in_threadpool(stream)
        ]

    async def run_both():
        return await asyncio.gather(consume("alpha"), consume("beta"))

    alpha_events, beta_events = asyncio.run(run_both())

    def tokens(events):
        return [
            event["content"]
            for event in events
            if event.get("type") == "token"
        ]

    assert tokens(alpha_events) == ["alpha:1", "alpha:2"]
    assert tokens(beta_events) == ["beta:1", "beta:2"]
    assert alpha_events[-1]["type"] == "done"
    assert beta_events[-1]["type"] == "done"
    assert streaming_enabled() is False


def test_blocking_send_message_does_not_enable_streaming_mode(monkeypatch):
    calls = {"stream": 0, "generate": 0}

    def fake_generate(request):
        calls["generate"] += 1
        return LLMResponse(
            content="blocking response",
            provider="groq",
            model="test-model",
            latency_ms=1,
            finish_reason="stop",
        )

    def fake_stream(request):
        calls["stream"] += 1
        raise AssertionError("stream() must not be used by send_message()")

    monkeypatch.setattr(router_module.llm_router, "generate", fake_generate)
    monkeypatch.setattr(router_module.llm_router, "stream", fake_stream)
    graph = FakeStreamingGraph(_llm_node)
    monkeypatch.setattr(chat_service_module, "chat_graph", graph)
    service, _messages, _repository = _make_service()

    response = service.send_message(
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        content="Say hello",
        idempotency_key=uuid4(),
        user_timezone="UTC",
    )

    assert response["assistant_message"]["content"] == "blocking response"
    assert calls == {"stream": 0, "generate": 1}
    assert streaming_enabled() is False
    assert graph.invoke_calls == 1
    assert graph.stream_calls == 0


def test_tokens_are_forwarded_in_provider_order_without_slicing(monkeypatch):
    chunks = ["A", "bc", "", "D", "efgh"]
    monkeypatch.setattr(
        router_module.llm_router,
        "stream",
        lambda request: iter([
            LLMStreamChunk(
                content=content,
                provider="openrouter",
                model="test-model",
                is_final=index == len(chunks) - 1,
            )
            for index, content in enumerate(chunks)
        ]),
    )
    graph = FakeStreamingGraph(_llm_node)
    monkeypatch.setattr(chat_service_module, "chat_graph", graph)
    service, _messages, _repository = _make_service()

    events = _collect_stream(service)
    tokens = [e["content"] for e in events if e.get("type") == "token"]

    assert tokens == ["A", "bc", "D", "efgh"]
    assert "".join(tokens) == "AbcDefgh"
    # No transport-level 120-character re-chunking.
    assert all(len(token) < 120 for token in tokens)
    assert all(e.get("type") != "error" for e in events)


def test_deterministic_route_emits_one_final_content_event(monkeypatch):
    graph = FakeStreamingGraph(_deterministic_node)
    monkeypatch.setattr(chat_service_module, "chat_graph", graph)
    service, _messages, _repository = _make_service()

    events = _collect_stream(service)
    tokens = [e["content"] for e in events if e.get("type") == "token"]

    assert tokens == ["Here is your capability summary."]
    assert events[-1]["type"] == "done"
    assert (
        events[-1]["response"]["assistant_message"]["content"]
        == tokens[0]
    )


def test_stream_events_are_allowlisted_and_do_not_leak_payloads():
    assert ChatService._sanitize_stream_event({
        "type": "status",
        "message": "Searching Gmail...",
        "payload": {"raw_email": "secret body"},
        "input": {"prompt": "hidden"},
    }) == {"type": "status", "message": "Searching Gmail..."}

    assert ChatService._sanitize_stream_event({
        "type": "token",
        "content": "safe text",
        "payload": {"raw": "nope"},
    }) == {"type": "token", "content": "safe text"}

    assert ChatService._sanitize_stream_event({
        "type": "replace",
        "content": "final",
    }) == {"type": "replace", "content": "final"}

    assert ChatService._sanitize_stream_event({
        "type": "tool_payload",
        "content": "should not pass",
    }) is None
    assert ChatService._sanitize_stream_event("not a dict") is None

    debug_payload = {
        "type": "task",
        "step": 1,
        "payload": {
            "name": "memory_retrieval_node",
            "input": {"secret": "never forward"},
        },
    }
    assert (
        ChatService._status_for_debug_event(debug_payload)
        == "Checking saved memories..."
    )
    assert ChatService._status_for_debug_event({
        "type": "task",
        "payload": {"name": "some_internal_node"},
    }) is None
    assert ChatService._status_for_debug_event({
        "type": "task_result",
        "payload": {"name": "memory_retrieval_node"},
    }) is None


def test_non_streaming_generate_path_is_preserved(monkeypatch):
    calls = []

    def fake_generate(request):
        calls.append(request)
        return LLMResponse(
            content="blocking response",
            provider="gemini",
            model="test-model",
            latency_ms=1,
            finish_reason="stop",
        )

    def forbidden_stream(request):
        raise AssertionError("stream() must not be called without streaming mode")

    monkeypatch.setattr(router_module.llm_router, "generate", fake_generate)
    monkeypatch.setattr(router_module.llm_router, "stream", forbidden_stream)

    response = generate_or_stream(
        LLMRequest(messages=[LLMMessage(role="user", content="hi")])
    )

    assert response.content == "blocking response"
    assert len(calls) == 1


class ProviderServerError(RuntimeError):
    status_code = 500


class FakeProvider:
    def __init__(self, name, chunks=(), error=None, fail_after_first=False):
        self.provider_name = name
        self.model = f"{name}-model"
        self.chunks = list(chunks)
        self.error = error
        self.fail_after_first = fail_after_first
        self.stream_calls = 0

    def stream(self, request):
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


class FakeRegistry:
    def __init__(self, providers):
        self.providers = providers

    def is_configured(self, name):
        return name in self.providers

    def get_provider(self, name):
        return self.providers[name]


def _manager(providers, priority):
    manager = FallbackManager(registry=FakeRegistry(providers))
    manager.provider_priority = priority
    return manager


def test_provider_fallback_occurs_before_first_token():
    failing = FakeProvider("groq", error=ProviderServerError("boom"))
    healthy = FakeProvider("openrouter", chunks=["ok", "!"])
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


def test_empty_stream_falls_back_before_first_token():
    empty = FakeProvider("groq", chunks=())
    healthy = FakeProvider("openrouter", chunks=["ok", "!"])
    manager = _manager(
        {"groq": empty, "openrouter": healthy},
        ["groq", "openrouter"],
    )

    received = list(
        manager.stream(
            LLMRequest(messages=[LLMMessage(role="user", content="hi")])
        )
    )

    assert "".join(chunk.content for chunk in received) == "ok!"
    assert empty.stream_calls == 1
    assert healthy.stream_calls == 1


def test_provider_failure_after_first_token_never_switches():
    partial = FakeProvider(
        "groq",
        chunks=["partial"],
        error=ProviderServerError("boom"),
        fail_after_first=True,
    )
    healthy = FakeProvider("openrouter", chunks=["should-not-appear"])
    manager = _manager(
        {"groq": partial, "openrouter": healthy},
        ["groq", "openrouter"],
    )

    received = []
    with pytest.raises(ProviderServerError):
        for chunk in manager.stream(
            LLMRequest(messages=[LLMMessage(role="user", content="hi")])
        ):
            received.append(chunk)

    assert "".join(chunk.content for chunk in received) == "partial"
    assert healthy.stream_calls == 0


def test_midstream_failure_marks_request_failed_and_keeps_no_assistant(monkeypatch):
    def failing_stream(request):
        def generator():
            yield LLMStreamChunk(
                content="partial",
                provider="groq",
                model="test-model",
            )
            raise ProviderServerError("stream broke")

        return generator()

    monkeypatch.setattr(router_module.llm_router, "stream", failing_stream)
    graph = FakeStreamingGraph(_llm_node)
    monkeypatch.setattr(chat_service_module, "chat_graph", graph)
    service, messages, repository = _make_service()

    events = []
    with pytest.raises(ProviderServerError):
        for event in service.stream_message(
            user_id=USER_ID,
            conversation_id=CONVERSATION_ID,
            content="Say hello",
            idempotency_key=uuid4(),
            user_timezone="UTC",
        ):
            events.append(event)

    assert [e["content"] for e in events if e.get("type") == "token"] == [
        "partial"
    ]
    assert repository.statuses[-1] == "failed"
    assert [m["role"] for m in messages.messages] == ["user"]
    assert streaming_enabled() is False


def test_client_disconnect_marks_unpersisted_request_failed(monkeypatch):
    delayed = DelayedStream()
    monkeypatch.setattr(router_module.llm_router, "stream", delayed.stream)
    graph = FakeStreamingGraph(_llm_node)
    monkeypatch.setattr(chat_service_module, "chat_graph", graph)
    service, messages, repository = _make_service()

    stream = service.stream_message(
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        content="Say hello",
        idempotency_key=uuid4(),
        user_timezone="UTC",
    )

    first = next(stream)
    assert first["type"] == "status"
    second = next(stream)
    assert second["type"] == "token"
    assert second["content"] == "Hel"

    # Simulate the client disconnecting mid-generation.
    stream.close()
    delayed.release.set()

    assert "completed" not in repository.statuses
    assert "failed" in repository.statuses
    assert [m["role"] for m in messages.messages] == ["user"]


def test_tenant_isolation_is_enforced_before_streaming():
    service, messages, repository = _make_service(owned=False)
    stream = service.stream_message(
        user_id=OTHER_USER_ID,
        conversation_id=CONVERSATION_ID,
        content="Say hello",
        idempotency_key=uuid4(),
        user_timezone="UTC",
    )

    with pytest.raises(LookupError):
        next(stream)

    assert messages.ownership_checks == [(OTHER_USER_ID, CONVERSATION_ID)]
    assert repository.statuses == []


def test_idempotent_replay_returns_one_assistant_message(monkeypatch):
    graph = FakeStreamingGraph(_deterministic_node)
    monkeypatch.setattr(chat_service_module, "chat_graph", graph)
    service, messages, repository = _make_service()

    idempotency_key = uuid4()
    first = service.send_message(
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        content="What can you do?",
        idempotency_key=idempotency_key,
        user_timezone="UTC",
    )
    second = service.send_message(
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        content="What can you do?",
        idempotency_key=idempotency_key,
        user_timezone="UTC",
    )

    assert first["assistant_message"]["id"] == second["assistant_message"]["id"]
    assert graph.invoke_calls == 1
    assert [m["role"] for m in messages.messages] == ["user", "assistant"]
    assert repository.statuses.count("completed") == 1


class FakeStreamingService:
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
            if not line.startswith("data:"):
                continue
            events.append(json.loads(line[5:].strip()))
    return events


def _response_payload():
    return {
        "conversation_id": CONVERSATION_ID,
        "user_message": {"id": uuid4(), "content": "hi"},
        "assistant_message": {
            "id": uuid4(),
            "content": "Hello world",
            "metadata": {"provider": "groq", "model": "test-model"},
        },
        "provider": "groq",
        "model": "test-model",
    }


def test_stream_endpoint_forwards_tokens_and_maps_done():
    response_payload = _response_payload()
    service = FakeStreamingService([
        {"type": "status", "message": "Searching Gmail..."},
        {"type": "token", "content": "Hello"},
        {"type": "token", "content": " world"},
        {"type": "done", "response": response_payload},
    ])

    response = chat_endpoint.chat_stream(
        payload=ChatRequest(
            conversation_id=CONVERSATION_ID,
            content="hi",
        ),
        user={"id": str(USER_ID), "user_metadata": {}},
        service=service,
        idempotency_key=uuid4(),
    )

    events = _parse_sse(response.body_iterator)

    assert events[0] == {"type": "status", "message": "Searching Gmail..."}
    assert [e["content"] for e in events if e["type"] == "token"] == [
        "Hello",
        " world",
    ]
    assert events[-1]["type"] == "done"
    assert events[-1]["message_id"] == str(
        response_payload["assistant_message"]["id"]
    )
    assert events[-1]["conversation_id"] == str(CONVERSATION_ID)
    # Raw persisted payloads are not exposed in the done event.
    assert "assistant_message" not in events[-1]
    assert service.calls[0]["user_id"] == USER_ID


def test_stream_endpoint_keeps_contextvar_tokens_in_their_own_context(
    monkeypatch,
):
    def fake_stream(request):
        return iter([
            LLMStreamChunk(
                content="Hel",
                provider="groq",
                model="test-model",
            ),
            LLMStreamChunk(
                content="lo",
                provider="groq",
                model="test-model",
                is_final=True,
            ),
        ])

    monkeypatch.setattr(router_module.llm_router, "stream", fake_stream)
    graph = FakeStreamingGraph(_llm_node)
    monkeypatch.setattr(chat_service_module, "chat_graph", graph)
    service, _messages, _repository = _make_service()

    response = chat_endpoint.chat_stream(
        payload=ChatRequest(
            conversation_id=CONVERSATION_ID,
            content="Say hello",
        ),
        user={"id": str(USER_ID), "user_metadata": {}},
        service=service,
        idempotency_key=uuid4(),
    )

    # StreamingResponse wraps the sync generator with iterate_in_threadpool,
    # so the first event and the remaining events are advanced in different
    # execution contexts.
    events = _parse_sse(response.body_iterator)

    assert [
        event["content"]
        for event in events
        if event.get("type") == "token"
    ] == ["Hel", "lo"]
    assert events[-1]["type"] == "done"
    assert not any(event.get("type") == "error" for event in events)
    assert streaming_enabled() is False


def test_stream_endpoint_returns_http_error_before_streaming():
    class FailingService:
        def stream_message(self, **_kwargs):
            raise LookupError("Conversation not found.")

    with pytest.raises(chat_endpoint.HTTPException) as excinfo:
        chat_endpoint.chat_stream(
            payload=ChatRequest(
                conversation_id=CONVERSATION_ID,
                content="hi",
            ),
            user={"id": str(USER_ID), "user_metadata": {}},
            service=FailingService(),
            idempotency_key=uuid4(),
        )

    assert excinfo.value.status_code == 404


def test_stream_endpoint_emits_partial_error_after_tokens():
    class FailingService:
        def stream_message(self, **_kwargs):
            yield {"type": "token", "content": "partial"}
            raise RuntimeError("provider exploded")

    response = chat_endpoint.chat_stream(
        payload=ChatRequest(
            conversation_id=CONVERSATION_ID,
            content="hi",
        ),
        user={"id": str(USER_ID), "user_metadata": {}},
        service=FailingService(),
        idempotency_key=uuid4(),
    )

    events = _parse_sse(response.body_iterator)

    assert events[0] == {"type": "token", "content": "partial"}
    assert events[-1]["type"] == "error"
    assert events[-1]["partial"] is True
    assert "provider exploded" not in events[-1]["message"]


def test_streaming_mode_context_is_required_for_real_streaming(monkeypatch):
    called = {"stream": 0, "generate": 0}

    def fake_stream(request):
        called["stream"] += 1
        return iter([
            LLMStreamChunk(
                content="x",
                provider="groq",
                model="m",
                is_final=True,
            )
        ])

    def fake_generate(request):
        called["generate"] += 1
        return LLMResponse(
            content="x",
            provider="groq",
            model="m",
            latency_ms=1,
        )

    monkeypatch.setattr(router_module.llm_router, "stream", fake_stream)
    monkeypatch.setattr(router_module.llm_router, "generate", fake_generate)

    # Outside a graph run the writer is unavailable, so the safe blocking path
    # is used even when streaming mode is enabled.
    with streaming_mode():
        generate_or_stream(
            LLMRequest(messages=[LLMMessage(role="user", content="hi")])
        )

    assert called == {"stream": 0, "generate": 1}


class KeyedRequestRepository(FakeRequestRepository):
    """Repository that remembers whether each key belongs to a failed request."""

    def __init__(self, failed_key):
        super().__init__()
        self.failed_key = failed_key
        self.created_keys = []

    def create_request(self, *, idempotency_key, **_kwargs):
        self.created_keys.append(idempotency_key)
        self.request_id = uuid4()
        self.status = (
            "failed" if idempotency_key == self.failed_key else "processing"
        )
        self.user_message_id = None
        self.assistant_message_id = None
        self.execution_started = False
        return self._record(), True


def _deterministic_stream(request):
    return iter([
        LLMStreamChunk(
            content="Hel",
            provider="groq",
            model="test-model",
        ),
        LLMStreamChunk(
            content="lo",
            provider="groq",
            model="test-model",
            is_final=True,
        ),
    ])


def test_failed_key_is_terminal_without_reexecution(monkeypatch):
    graph = FakeStreamingGraph(_llm_node)
    monkeypatch.setattr(chat_service_module, "chat_graph", graph)
    service, messages, repository = _make_service()
    repository.request_id = uuid4()
    repository.status = "failed"

    with pytest.raises(ChatRequestFailedError, match="previously failed"):
        list(service.stream_message(
            user_id=USER_ID,
            conversation_id=CONVERSATION_ID,
            content="retry me",
            idempotency_key=uuid4(),
            user_timezone="UTC",
        ))

    assert graph.stream_calls == 0
    assert graph.invoke_calls == 0
    assert repository.status == "failed"
    assert repository.statuses == []
    assert messages.messages == []


def test_fresh_key_creates_distinct_request_after_terminal_failure(monkeypatch):
    monkeypatch.setattr(router_module.llm_router, "stream", _deterministic_stream)
    graph = FakeStreamingGraph(_llm_node)
    monkeypatch.setattr(chat_service_module, "chat_graph", graph)
    service, messages, _repository = _make_service()
    failed_key = uuid4()
    fresh_key = uuid4()
    keyed_repository = KeyedRequestRepository(failed_key)
    service.request_repository = keyed_repository

    with pytest.raises(ChatRequestFailedError):
        list(service.stream_message(
            user_id=USER_ID,
            conversation_id=CONVERSATION_ID,
            content="retry me",
            idempotency_key=failed_key,
            user_timezone="UTC",
        ))

    events = list(service.stream_message(
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        content="retry me",
        idempotency_key=fresh_key,
        user_timezone="UTC",
    ))

    assert keyed_repository.created_keys == [failed_key, fresh_key]
    assert graph.stream_calls == 1
    assert events[-1]["type"] == "done"
    assert keyed_repository.status == "completed"
    assert [m["role"] for m in messages.messages] == ["user", "assistant"]


def test_stream_endpoint_maps_failed_key_to_terminal_conflict(monkeypatch):
    graph = FakeStreamingGraph(_llm_node)
    monkeypatch.setattr(chat_service_module, "chat_graph", graph)
    service, _messages, repository = _make_service()
    repository.request_id = uuid4()
    repository.status = "failed"

    with pytest.raises(HTTPException) as exc_info:
        chat_endpoint.chat_stream(
            payload=ChatRequest(
                conversation_id=CONVERSATION_ID,
                content="retry me",
            ),
            user={"id": str(USER_ID), "user_metadata": {}},
            service=service,
            idempotency_key=uuid4(),
        )

    assert exc_info.value.status_code == 409
    assert "previously failed" in exc_info.value.detail
    assert "new Idempotency-Key" in exc_info.value.detail
    assert graph.stream_calls == 0
