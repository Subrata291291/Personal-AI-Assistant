import logging
from functools import wraps
from time import perf_counter

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import InMemorySaver

from app.workflows.chat_state import ChatContext, ChatState
from app.workflows.chat_nodes import (
    capabilities_node,
    integration_unavailable_node,
    llm_node,
)
from app.workflows.gmail_agent_nodes import (
    gmail_agent_node,
    gmail_intent_router_node,
    route_after_assistant_intent,
)
from app.workflows.calendar_agent_node import calendar_agent_node
from app.workflows.memory_retrieval_node import (
    memory_retrieval_node,
)
from app.workflows.memory_update_node import memory_update_node
from app.workflows.autonomous_memory_node import autonomous_memory_node
from app.core.request_metrics import record_timing, request_id


logger = logging.getLogger(__name__)

ASSISTANT_ROUTE_NODES = {
    "general": "memory_retrieval",
    "daily_planning": "memory_retrieval",
    "memory_update": "memory_update",
    "capabilities": "capabilities",
    "gmail": "gmail_agent",
    "google_drive": "integration_unavailable",
    "google_calendar": "calendar_agent",
    "google_contacts": "integration_unavailable",
    "tasks": "integration_unavailable",
}


def _timed_node(name, node):
    @wraps(node)
    def wrapped(*args, **kwargs):
        started_at = perf_counter()
        try:
            return node(*args, **kwargs)
        finally:
            duration_ms = (perf_counter() - started_at) * 1000
            record_timing(name, duration_ms)
            logger.info(
                "chat_node_completed | request_id=%s | node=%s "
                "| duration_ms=%.2f",
                request_id(),
                name,
                duration_ms,
            )

    return wrapped


# In-memory checkpoint storage
memory_saver = InMemorySaver()


def build_chat_graph():

    # Create graph builder
    builder = StateGraph(
        ChatState,
        context_schema=ChatContext,
    )

    # Register nodes
    builder.add_node(
        "memory_retrieval",
        _timed_node("memory_retrieval", memory_retrieval_node),
    )
    builder.add_node(
        "autonomous_memory",
        _timed_node("autonomous_memory", autonomous_memory_node),
    )
    builder.add_node(
        "memory_update",
        _timed_node("memory_update", memory_update_node),
    )

    builder.add_node(
        "intent_router",
        _timed_node("intent_router", gmail_intent_router_node),
    )

    builder.add_node(
        "llm",
        _timed_node("llm", llm_node),
    )

    builder.add_node(
        "capabilities",
        _timed_node("capabilities", capabilities_node),
    )

    builder.add_node(
        "gmail_agent",
        _timed_node("gmail_agent", gmail_agent_node),
    )

    builder.add_node(
        "calendar_agent",
        _timed_node("calendar_agent", calendar_agent_node),
    )

    builder.add_node(
        "integration_unavailable",
        _timed_node(
            "integration_unavailable",
            integration_unavailable_node,
        ),
    )

    # Define execution flow
    builder.add_edge(START, "autonomous_memory")

    builder.add_conditional_edges(
        "autonomous_memory",
        lambda state: (
            "memory_update"
            if state.get("assistant_route") == "memory_update"
            else "intent_router"
        ),
        {
            "memory_update": "memory_update",
            "intent_router": "intent_router",
        },
    )

    builder.add_conditional_edges(
        "intent_router",
        route_after_assistant_intent,
        ASSISTANT_ROUTE_NODES,
    )

    builder.add_edge(
        "memory_retrieval",
        "llm",
    )

    builder.add_edge(
        "llm",
        END,
    )

    builder.add_edge(
        "memory_update",
        END,
    )

    builder.add_edge(
        "gmail_agent",
        END,
    )

    builder.add_edge(
        "calendar_agent",
        END,
    )

    builder.add_edge(
        "capabilities",
        END,
    )

    # Compile graph with checkpointer
    graph = builder.compile(
        checkpointer=memory_saver
    )

    return graph


# Shared compiled graph
chat_graph = build_chat_graph()