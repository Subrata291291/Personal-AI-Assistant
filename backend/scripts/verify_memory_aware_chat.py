import uuid

from app.db.session import SessionLocal
from app.services.memory_service import MemoryService
from app.workflows.chat_graph import chat_graph


USER_ID = uuid.UUID("e82dde04-5dd5-4935-8c73-1881bc20b056")

TEST_MEMORY = (
    "My favorite programming language for this test is Rust."
)

TEST_QUESTION = (
    "Which programming language did I say I prefer "
    "for this test? Answer with the language name."
)


def main():
    session = SessionLocal()
    memory_id = None

    try:
        # Create temporary semantic memory.
        memory = MemoryService(session).create_memory(
            user_id=USER_ID,
            memory_type="semantic",
            content=TEST_MEMORY,
            importance=5,
            confidence=1.0,
        )

        memory_id = memory.id

        print("\nMEMORY-AWARE CHAT VERIFICATION")
        print("=" * 60)
        print("Temporary memory:", memory_id)

        initial_state = {
            "user_id": str(USER_ID),
            "conversation_id": str(uuid.uuid4()),
            "user_message": TEST_QUESTION,
            "assistant_response": "",
            "provider": "",
            "model": "",
            "messages": [],
        }

        config = {
            "configurable": {
                "thread_id": str(uuid.uuid4()),
            }
        }

        # Execute the complete LangGraph workflow.
        result = chat_graph.invoke(
            initial_state,
            config=config,
        )

        answer = result.get("assistant_response", "")

        print("\nRetrieved memories:")
        for item in result.get("retrieved_memories", []):
            print(
                item["memory_type"],
                item["similarity"],
                item["content"],
            )

        print("\nAssistant response:")
        print(answer)

        print("\nProvider:", result.get("provider"))
        print("Model:", result.get("model"))

        print(
            "Memory context available:",
            bool(result.get("memory_context")),
        )

        # Basic factual verification.
        passed = (
            bool(result.get("memory_context"))
            and "rust" in answer.casefold()
        )

        print("\nResult:", "PASS" if passed else "NEEDS REVIEW")
        print("=" * 60)

    finally:
        # Delete only the temporary memory.
        if memory_id is not None:
            deleted = MemoryService(session).delete_memory(
                user_id=USER_ID,
                memory_id=memory_id,
            )
            print("\nTemporary memory cleanup:", deleted)

        session.close()


if __name__ == "__main__":
    main()
