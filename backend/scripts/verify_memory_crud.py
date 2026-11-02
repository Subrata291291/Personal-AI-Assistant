import uuid
from datetime import datetime, timedelta, timezone

from app.db.session import SessionLocal
from app.services.memory_service import MemoryService


USER_A = uuid.UUID("e82dde04-5dd5-4935-8c73-1881bc20b056")
USER_B = uuid.UUID("76f05bd1-9a77-4b03-943e-7ea01b4b58dc")


def main():
    db = SessionLocal()
    service = MemoryService(db)
    memory_id = None

    try:
        print("\n1. CREATE MEMORY")

        memory = service.create_memory(
            user_id=USER_A,
            memory_type="long_term",
            content="Temporary memory CRUD verification record.",
            importance=7,
            confidence=0.95,
            metadata={"test": True, "source": "manual_verification"},
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        )

        memory_id = memory.id

        assert memory.id is not None
        assert memory.user_id == USER_A
        assert memory.embedding is not None
        assert len(memory.embedding) == 768

        print("CREATE: PASS")
        print("Automatic embedding dimensions:", len(memory.embedding))

        print("\n2. GET MEMORY")

        fetched = service.get_memory(USER_A, memory_id)

        assert fetched is not None
        assert fetched.content == "Temporary memory CRUD verification record."
        print("GET: PASS")

        print("\n3. LIST MEMORIES")

        memories = service.list_memories(
            user_id=USER_A,
            memory_type="long_term",
            limit=100,
        )

        assert any(item.id == memory_id for item in memories)
        print("LIST: PASS")

        print("\n4. UPDATE CONTENT")

        updated = service.update_memory(
            user_id=USER_A,
            memory_id=memory_id,
            content="Updated temporary verification record.",
            importance=8,
        )

        assert updated is not None
        assert updated.content == "Updated temporary verification record."
        assert updated.importance == 8
        assert updated.metadata_ == {"test": True, "source": "manual_verification"}
        assert updated.expires_at is not None
        print("UPDATE WITHOUT CLEARING OPTIONAL FIELDS: PASS")

        print("\n5. CLEAR OPTIONAL FIELDS")

        cleared = service.update_memory(
            user_id=USER_A,
            memory_id=memory_id,
            metadata=None,
            expires_at=None,
            embedding=None,
        )

        assert cleared is not None
        assert cleared.metadata_ is None
        assert cleared.expires_at is None
        assert cleared.embedding is None
        print("EXPLICIT NONE CLEARING: PASS")

        print("\n6. USER ISOLATION")

        other_user_memory = service.get_memory(USER_B, memory_id)

        assert other_user_memory is None

        other_user_memories = service.list_memories(
            user_id=USER_B,
            memory_type="long_term",
            limit=100,
        )

        assert not any(item.id == memory_id for item in other_user_memories)
        print("CROSS-USER ACCESS BLOCKED: PASS")

        print("\n7. DELETE MEMORY")

        deleted = service.delete_memory(USER_A, memory_id)

        assert deleted is True

        after_delete = service.get_memory(USER_A, memory_id)

        assert after_delete is None
        print("DELETE: PASS")

        memory_id = None

        print("\nALL MEMORY CRUD CHECKS: PASS")

    finally:
        if memory_id is not None:
            try:
                service.delete_memory(USER_A, memory_id)
                print("\nTemporary test memory cleanup attempted.")
            except Exception as cleanup_error:
                print("WARNING: Cleanup failed:", type(cleanup_error).__name__)

        db.close()


if __name__ == "__main__":
    main()
