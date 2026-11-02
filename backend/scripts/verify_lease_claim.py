
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

    # Create a fresh request.
    record, created = repository.create_request(
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        idempotency_key=idempotency_key,
    )

    print("Request created:", created)
    print("Initial status:", record["status"])

    # Worker A claims the request.
    worker_a = repository.claim_request(
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        idempotency_key=idempotency_key,
        worker_id="worker-A",
        lease_seconds=120,
    )

    print("\nWorker A claim:", worker_a is not None)

    if worker_a is None:
        raise RuntimeError("Worker A could not claim the request.")

    print("Worker ID:", worker_a["worker_id"])
    print("Attempt count:", worker_a["attempt_count"])

    # Worker B tries to claim the same request.
    worker_b = repository.claim_request(
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        idempotency_key=idempotency_key,
        worker_id="worker-B",
        lease_seconds=120,
    )

    print("\nWorker B claim:", worker_b is not None)

    # Retrieve the final database state.
    final_record = repository.get_request(
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        idempotency_key=idempotency_key,
    )

    print("\nFinal database state:")
    print("Worker ID:", final_record["worker_id"])
    print("Attempt count:", final_record["attempt_count"])
    print("Status:", final_record["status"])

    print(
        "\nSingle worker ownership:",
        worker_b is None
        and final_record["worker_id"] == "worker-A"
        and final_record["attempt_count"] == 1,
    )


if __name__ == "__main__":
    main()
