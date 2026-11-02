import uuid

from app.db.session import SessionLocal
from app.services.memory_service import MemoryService
from app.memory.deduplication.duplicate_detector import DuplicateDetector


USER_ID = uuid.UUID("e82dde04-5dd5-4935-8c73-1881bc20b056")

TEST_CONTENT = (
    "My temporary duplicate detection test project uses "
    "FastAPI and PostgreSQL."
)

PARAPHRASED_CONTENT = (
    "I am building a test application with PostgreSQL "
    "as its database and FastAPI as its backend."
)

UNRELATED_CONTENT = (
    "I enjoy learning about astronomy and distant galaxies."
)


def main():
    session = SessionLocal()
    memory_id = None

    try:
        memory_service = MemoryService(session)
        detector = DuplicateDetector(session)

        print("\nDUPLICATE DETECTOR VERIFICATION")
        print("=" * 60)

        # Create one temporary memory.
        memory = memory_service.create_memory(
            user_id=USER_ID,
            memory_type="semantic",
            content=TEST_CONTENT,
            importance=5,
            confidence=1.0,
        )

        memory_id = memory.id
        print("\nTemporary memory created:", memory_id)

        # Test 1: Exact duplicate.
        exact_result = detector.check_duplicate(
            user_id=USER_ID,
            memory_type="semantic",
            content=TEST_CONTENT,
        )

        exact_pass = (
            exact_result.status == "strong_duplicate"
            and exact_result.is_duplicate
        )

        print("\nTest 1: Exact duplicate")
        print("Status:", exact_result.status)
        print("Is duplicate:", exact_result.is_duplicate)
        print("Result:", "PASS" if exact_pass else "FAIL")

        # Inspect semantic similarity before applying duplicate threshold.
        raw_results = memory_service.search_similar_memories(
            user_id=USER_ID,
            query=PARAPHRASED_CONTENT,
            limit=5,
            min_similarity=0.70,
        )

        print("\nSemantic similarity calibration:")

        for existing_memory, similarity in raw_results:
            print("Memory type:", existing_memory.memory_type)
            print("Similarity:", round(similarity, 4))

        # Test 2: Semantic paraphrase.
        semantic_result = detector.check_duplicate(
            user_id=USER_ID,
            memory_type="semantic",
            content=PARAPHRASED_CONTENT,
        )

        print("\nTest 2: Semantic paraphrase")
        print("Status:", semantic_result.status)
        print("Is duplicate:", semantic_result.is_duplicate)
        print("Reason:", semantic_result.reason)

        for candidate in semantic_result.candidates:
            print("Similarity:", round(candidate.similarity, 4))

        print("Review result: OBSERVATION ONLY")

        # Test 3: Unrelated information.
        unrelated_result = detector.check_duplicate(
            user_id=USER_ID,
            memory_type="semantic",
            content=UNRELATED_CONTENT,
        )

        unrelated_pass = (
            unrelated_result.status == "new_memory"
            and not unrelated_result.is_duplicate
        )

        print("\nTest 3: Unrelated information")
        print("Status:", unrelated_result.status)
        print("Is duplicate:", unrelated_result.is_duplicate)
        print("Result:", "PASS" if unrelated_pass else "FAIL")

        print("\n" + "=" * 60)
        print(
            "DETERMINISTIC RESULT:",
            f"{int(exact_pass) + int(unrelated_pass)}/2 passed",
        )
        print("=" * 60)

    finally:
        # Remove only the temporary memory created by this script.
        if memory_id is not None:
            deleted = MemoryService(session).delete_memory(
                user_id=USER_ID,
                memory_id=memory_id,
            )
            print("\nTemporary memory cleanup:", deleted)

        session.close()


if __name__ == "__main__":
    main()
