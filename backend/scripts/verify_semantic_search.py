import uuid

from app.db.session import SessionLocal
from app.services.memory_service import MemoryService


USER_ID = uuid.UUID(
    "e82dde04-5dd5-4935-8c73-1881bc20b056"
)

SIMILARITY_THRESHOLD = 0.70

TEST_MEMORIES = [
    "I prefer travelling with my family.",
    "I work as a frontend developer.",
    "I enjoy learning artificial intelligence.",
]

POSITIVE_QUERIES = [
    (
        "Who do I usually travel with?",
        "I prefer travelling with my family.",
    ),
    (
        "What kind of development work do I do?",
        "I work as a frontend developer.",
    ),
]

NEGATIVE_QUERIES = [
    "What is the capital of Japan?",
]


def main():
    db = SessionLocal()
    service = MemoryService(db)

    created_ids = []

    positive_passed = 0
    negative_passed = 0

    try:
        # ==========================================
        # 1. CREATE TEST MEMORIES
        # ==========================================

        print("\n1. CREATING TEST MEMORIES")

        for content in TEST_MEMORIES:
            memory = service.create_memory(
                user_id=USER_ID,
                memory_type="long_term",
                content=content,
                importance=7,
                confidence=0.95,
                metadata={
                    "test": "threshold_evaluation"
                },
            )

            created_ids.append(memory.id)

            assert memory.embedding is not None
            assert len(memory.embedding) == 768

            print("Created:", memory.content)
            print("Embedding dimensions:", len(memory.embedding))

        print("\nMEMORY CREATION: PASS")

        # ==========================================
        # 2. POSITIVE RETRIEVAL TESTS
        # ==========================================

        print("\n2. POSITIVE RETRIEVAL TESTS")

        for query, expected_content in POSITIVE_QUERIES:

            print("\nQuery:", query)

            results = service.search_similar_memories(
                user_id=USER_ID,
                query=query,
                limit=3,
            )

            assert results, "No memories retrieved."

            for index, (memory, score) in enumerate(
                results,
                start=1,
            ):
                print(
                    f"{index}. {memory.content} "
                    f"| Similarity: {score:.6f}"
                )

            top_memory, top_score = results[0]

            accepted = (
                top_memory.content == expected_content
                and top_score >= SIMILARITY_THRESHOLD
            )

            assert accepted, (
                f"Positive retrieval failed.\n"
                f"Expected: {expected_content}\n"
                f"Actual: {top_memory.content}\n"
                f"Score: {top_score:.6f}"
            )

            positive_passed += 1
            print("POSITIVE RETRIEVAL: PASS")

        # ==========================================
        # 3. NEGATIVE RETRIEVAL TESTS
        # ==========================================

        print("\n3. NEGATIVE RETRIEVAL TESTS")

        for query in NEGATIVE_QUERIES:

            print("\nQuery:", query)

            results = service.search_similar_memories(
                user_id=USER_ID,
                query=query,
                limit=3,
            )

            if not results:
                print("No memories retrieved.")
                negative_passed += 1
                print("NEGATIVE RETRIEVAL: PASS")
                continue

            top_memory, top_score = results[0]

            print("Top candidate:", top_memory.content)
            print(f"Similarity: {top_score:.6f}")

            if top_score < SIMILARITY_THRESHOLD:
                print(
                    "Decision: REJECTED "
                    "(below similarity threshold)"
                )

                negative_passed += 1
                print("NEGATIVE RETRIEVAL: PASS")

            else:
                print(
                    "Decision: ACCEPTED "
                    "(above similarity threshold)"
                )

                print(
                    "WARNING: The current threshold did not "
                    "reject this unrelated query."
                )

        # ==========================================
        # 4. EVALUATION SUMMARY
        # ==========================================

        print("\n" + "=" * 55)
        print("RETRIEVAL EVALUATION SUMMARY")
        print("=" * 55)

        print(
            f"Positive tests: "
            f"{positive_passed}/{len(POSITIVE_QUERIES)}"
        )

        print(
            f"Negative tests: "
            f"{negative_passed}/{len(NEGATIVE_QUERIES)}"
        )

        print(
            f"Experimental threshold: "
            f"{SIMILARITY_THRESHOLD}"
        )

        if (
            positive_passed == len(POSITIVE_QUERIES)
            and negative_passed == len(NEGATIVE_QUERIES)
        ):
            print("\nRETRIEVAL QUALITY CHECK: PASS")
        else:
            print("\nRETRIEVAL QUALITY CHECK: NEEDS REVIEW")

        # ==========================================
        # 5. CLEANUP
        # ==========================================

    finally:
        print("\n5. CLEANING TEST MEMORIES")

        for memory_id in created_ids:
            try:
                deleted = service.delete_memory(
                    user_id=USER_ID,
                    memory_id=memory_id,
                )

                if deleted:
                    print("Deleted:", memory_id)
                else:
                    print("Already absent:", memory_id)

            except Exception as error:
                print(
                    "CLEANUP WARNING:",
                    memory_id,
                    type(error).__name__,
                )

        db.close()


if __name__ == "__main__":
    main()
