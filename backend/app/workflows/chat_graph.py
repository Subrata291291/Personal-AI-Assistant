from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import InMemorySaver

from app.workflows.chat_state import ChatContext, ChatState
from app.workflows.chat_nodes import llm_node
from app.workflows.gmail_agent_nodes import (
    gmail_agent_node,
    gmail_intent_router_node,
    route_after_gmail_intent,
)
from app.workflows.memory_retrieval_node import (
    memory_retrieval_node,
)


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
        memory_retrieval_node,
    )

    builder.add_node(
        "intent_router",
        gmail_intent_router_node,
    )

    builder.add_node(
        "llm",
        llm_node,
    )

    builder.add_node(
        "gmail_agent",
        gmail_agent_node,
    )

    # Define execution flow
    builder.add_edge(
        START,
        "memory_retrieval",
    )

    builder.add_edge(
        "memory_retrieval",
        "intent_router",
    )

    builder.add_conditional_edges(
        "intent_router",
        route_after_gmail_intent,
        {
            "general": "llm",
            "gmail_read": "gmail_agent",
            "gmail_response": "gmail_agent",
        },
    )

    builder.add_edge(
        "llm",
        END,
    )

    builder.add_edge(
        "gmail_agent",
        END,
    )

    # Compile graph with checkpointer
    graph = builder.compile(
        checkpointer=memory_saver
    )

    return graph


# Shared compiled graph
chat_graph = build_chat_graph()