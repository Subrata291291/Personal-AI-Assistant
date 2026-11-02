import uuid

from app.db.session import SessionLocal
from app.memory.memory_pipeline import MemoryPipeline
from app.services.memory_service import MemoryService


USER_ID = uuid.UUID("e82dde04-5dd5-4935-8c73-1881bc20b056")

TEST_CONTENT = (
    "My temporary memory pipeline verification project "
    "uses FastAPI with PostgreSQL."
)


def main():
    session = SessionLocal()
    memory_id = None

    try:
        pipeline = MemoryPipeline(session)

        print("\nMEMORY PIPELINE VERIFICATION")
        print("=" * 60)

        # Test 1: Memory disabled.
        disabled_result = pipeline.process(
            user_id=USER_ID,
            content=TEST_CONTENT,
            memory_enabled=False,
        )

        disabled_pass = (
            disabled_result.status == "disabled"
        )

        print("\nTest 1: Memory disabled")
        print("Status:", disabled_result.status)
        print("Result:", "PASS" if disabled_pass else "FAIL")

        # Test 2: Automatic memory storage enabled.
        stored_result = pipeline.process(
            user_id=USER_ID,
            content=TEST_CONTENT,
            memory_enabled=True,
        )

        stored_pass = (
            stored_result.status == "stored"
            and stored_result.memory_id is not None
        )

        memory_id = stored_result.memory_id

        print("\nTest 2: New memory")
        print("Status:", stored_result.status)
        print("Memory type:", stored_result.memory_type)
        print("Confidence:", stored_result.confidence)
        print("Memory ID:", stored_result.memory_id)
        print("Result:", "PASS" if stored_pass else "FAIL")

        if not stored_pass:
            print("\nPipeline did not store the memory.")
            return

        # Test 3: Verify database record.
        saved_memory = MemoryService(session).get_memory(
            user_id=USER_ID,
            memory_id=memory_id,
        )

        database_pass = (
            saved_memory is not None
            and saved_memory.content == TEST_CONTENT
        )

        print("\nTest 3: Database verification")
        print("Record exists:", saved_memory is not None)
        print("Result:", "PASS" if database_pass else "FAIL")

        # Test 4: Submit the same information again.
        duplicate_result = pipeline.process(
            user_id=USER_ID,
            content=TEST_CONTENT,
            memory_enabled=True,
        )

        duplicate_pass = (
            duplicate_result.status == "duplicate"
        )

        print("\nTest 4: Repeated memory")
        print("Status:", duplicate_result.status)
        print("Reason:", duplicate_result.reason)
        print("Result:", "PASS" if duplicate_pass else "FAIL")

        passed = sum([
            disabled_pass,
            stored_pass,
            database_pass,
            duplicate_pass,
        ])

        print("\n" + "=" * 60)
        print(f"RESULT: {passed}/4 passed")
        print("=" * 60)

    finally:
        # Delete only the temporary memory created by this script.
        if memory_id is not None:
            deleted = MemoryService(session).delete_memory(
                user_id=USER_ID,
                memory_id=memory_id,
            )
            print("\nTemporary memory cleanup:", deleted)

        session.close()


if __name__ == "__main__":
    main()
