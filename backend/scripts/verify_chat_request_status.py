
import sys
from pathlib import Path
from uuid import UUID

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from app.repositories.chat_request_repository import (
    ChatRequestRepository,
)


USER_ID = UUID("e82dde04-5dd5-4935-8c73-1881bc20b056")

CONVERSATION_ID = UUID(
    "f6416a8d-47ab-4688-94d4-fa7cfedcae2a"
)

IDEMPOTENCY_KEY = UUID(
    "b4e56d7f-fc0a-459a-b986-4d04d704d71a"
)


def main():
    repository = ChatRequestRepository()

    print("Checking existing request...")

    existing = repository.get_request(
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        idempotency_key=IDEMPOTENCY_KEY,
    )

    if existing is None:
        raise RuntimeError("Verification request not found.")

    print("Current status:", existing["status"])

    if existing["status"] != "processing":
        raise RuntimeError(
            "Expected processing status before transition."
        )

    updated = repository.update_status(
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        idempotency_key=IDEMPOTENCY_KEY,
        status="completed",
    )

    if updated is None:
        raise RuntimeError("Status update failed.")

    print("\nAfter status update:")
    print("Updated status:", updated["status"])

    # Verify completed requests cannot be transitioned again.
    second_update = repository.update_status(
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        idempotency_key=IDEMPOTENCY_KEY,
        status="failed",
    )

    print(
        "Second transition blocked:",
        second_update is None,
    )

    final_record = repository.get_request(
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        idempotency_key=IDEMPOTENCY_KEY,
    )

    print("Final status:", final_record["status"])


if __name__ == "__main__":
    main()
