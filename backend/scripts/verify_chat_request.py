
import sys
from pathlib import Path
from uuid import uuid4, UUID

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

    # Generate one unique key for this verification.
    idempotency_key = uuid4()

    print("Idempotency Key:", idempotency_key)

    # First request
    first_record, first_created = repository.create_request(
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        idempotency_key=idempotency_key,
    )

    print("\nFirst request:")
    print("Created:", first_created)
    print("Status:", first_record["status"])

    # Repeat the exact same request
    second_record, second_created = repository.create_request(
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        idempotency_key=idempotency_key,
    )

    print("\nSecond request:")
    print("Created:", second_created)
    print("Status:", second_record["status"])

    print("\nVerification:")
    print(
        "Same request ID:",
        first_record["id"] == second_record["id"],
    )
    print(
        "Duplicate prevented:",
        first_created and not second_created,
    )


if __name__ == "__main__":
    main()
