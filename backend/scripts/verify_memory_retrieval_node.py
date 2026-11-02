import uuid

from app.db.session import SessionLocal
from app.services.memory_service import MemoryService
from app.workflows.memory_retrieval_node import memory_retrieval_node


USER_A = uuid.UUID("e82dde04-5dd5-4935-8c73-1881bc20b056")
USER_B = uuid.UUID("76f05bd1-9a77-4b03-943e-7ea01b4b58dc")

TEST_CONTENT_A = (
    "My temporary retrieval test project uses FastAPI "
    "and PostgreSQL."
)

TEST_CONTENT_B = (
    "My temporary project uses Django and MySQL."
)


def main():
    session = SessionLocal()

    memory_a_id = None
    memory_b_id = None

    try:
        service = MemoryService(session)

        # Create temporary memory for User A.
        memory_a = service.create_memory(
            user_id=USER_A,
            memory_type="semantic",
            content=TEST_CONTENT_A,
        )
        memory_a_id = memory_a.id

        # Create temporary memory for User B.
        memory_b = service.create_memory(
            user_id=USER_B,
            memory_type="semantic",
            content=TEST_CONTENT_B,
        )
        memory_b_id = memory_b.id

        print("\nMEMORY RETRIEVAL NODE VERIFICATION")
        print("=" * 60)

        # Test 1: User A retrieves their own memory.
        result_a = memory_retrieval_node({
            "user_id": str(USER_A),
            "conversation_id": str(uuid.uuid4()),
            "user_message": "Which database does my project use?",
            "assistant_response": "",
            "provider": "",
            "model": "",
            "messages": [],
        })

        contents_a = [
            item["content"]
            for item in result_a["retrieved_memories"]
        ]

        test_1 = any(
            memory_a_id == uuid.UUID(item["memory_id"])
            for item in result_a["retrieved_memories"]
        )

        print("\nTest 1: User A retrieves own memory")
        print("Retrieved count:", len(contents_a))
        print("Memory context:", result_a["memory_context"])
        print("Result:", "PASS" if test_1 else "FAIL")

        # Test 2: User A must not retrieve User B's memory.
        test_2 = all(
            uuid.UUID(item["memory_id"]) != memory_b_id
            for item in result_a["retrieved_memories"]
        )

        print("\nTest 2: User isolation")
        print("User B memory excluded:", test_2)
        print("Result:", "PASS" if test_2 else "FAIL")

        # Test 3: Unrelated query should return empty context.
        result_empty = memory_retrieval_node({
            "user_id": str(USER_A),
            "conversation_id": str(uuid.uuid4()),
            "user_message": (
                "What is the capital of Japan?"
            ),
            "assistant_response": "",
            "provider": "",
            "model": "",
            "messages": [],
        })

        test_3 = (
            result_empty["retrieved_memories"] == []
            and result_empty["memory_context"] == ""
        )

        print("\nTest 3: No relevant memory")
        print("Retrieved count:", len(result_empty["retrieved_memories"]))
        print("Empty context:", result_empty["memory_context"] == "")
        print("Result:", "PASS" if test_3 else "FAIL")

        passed = sum([test_1, test_2, test_3])

        print("\n" + "=" * 60)
        print(f"RESULT: {passed}/3 passed")
        print("=" * 60)

    finally:
        # Delete only the two temporary memories.
        if memory_a_id is not None:
            print(
                "\nUser A cleanup:",
                MemoryService(session).delete_memory(
                    USER_A, memory_a_id
                ),
            )

        if memory_b_id is not None:
            print(
                "User B cleanup:",
                MemoryService(session).delete_memory(
                    USER_B, memory_b_id
                ),
            )

        session.close()


if __name__ == "__main__":
    main()
