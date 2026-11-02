from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import InMemorySaver

from app.workflows.chat_state import ChatState
from app.workflows.chat_nodes import llm_node
from app.workflows.memory_retrieval_node import (
    memory_retrieval_node,
)


# In-memory checkpoint storage
memory_saver = InMemorySaver()


def build_chat_graph():

    # Create graph builder
    builder = StateGraph(ChatState)

    # Register nodes
    builder.add_node(
        "memory_retrieval",
        memory_retrieval_node,
    )

    builder.add_node(
        "llm",
        llm_node,
    )

    # Define execution flow
    builder.add_edge(
        START,
        "memory_retrieval",
    )

    builder.add_edge(
        "memory_retrieval",
        "llm",
    )

    builder.add_edge(
        "llm",
        END,
    )

    # Compile graph with checkpointer
    graph = builder.compile(
        checkpointer=memory_saver
    )

    return graph


# Shared compiled graph
chat_graph = build_chat_graph()