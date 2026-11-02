
import sys
from pathlib import Path
from uuid import UUID, uuid4

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from app.repositories.chat_request_repository import (
    ChatRequestRepository,
)


USER_ID = UUID("e82dde04-5dd5-4935-8c73-1881bc20b056")

CONVERSATION_ID = UUID(
    "f6416a8d-47ab-4688-94d4-fa7cfedcae2a"
)


def main():
    repository = ChatRequestRepository()

    idempotency_key = uuid4()

    print("Verification key:", idempotency_key)

    # Step 1: Create a fresh request.
    record, created = repository.create_request(
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        idempotency_key=idempotency_key,
    )

    print("\nRequest created:", created)
    print("Initial status:", record["status"])

    if not created or record["status"] != "processing":
        raise RuntimeError("Initial request creation failed.")

    # Step 2: Simulate a known failure.
    failed_record = repository.update_status(
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        idempotency_key=idempotency_key,
        status="failed",
        error_message="Simulated verification failure",
    )

    if failed_record is None:
        raise RuntimeError("Failed status transition did not occur.")

    print("\nAfter simulated failure:")
    print("Status:", failed_record["status"])
    print("Error:", failed_record["error_message"])

    # Step 3: A failed request must not transition directly
    # to completed through the current repository method.
    completed_attempt = repository.update_status(
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        idempotency_key=idempotency_key,
        status="completed",
    )

    print(
        "\nFailed-to-completed transition blocked:",
        completed_attempt is None,
    )

    # Step 4: Duplicate request must return the same failed row.
    duplicate_record, duplicate_created = (
        repository.create_request(
            user_id=USER_ID,
            conversation_id=CONVERSATION_ID,
            idempotency_key=idempotency_key,
        )
    )

    print("\nDuplicate request created:", duplicate_created)
    print(
        "Same request ID:",
        duplicate_record["id"] == record["id"],
    )
    print("Existing status:", duplicate_record["status"])

    if duplicate_record["status"] != "failed":
        raise RuntimeError("Expected existing failed status.")

    print("\nFailure lifecycle verification completed.")


if __name__ == "__main__":
    main()
