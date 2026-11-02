
import sys
from pathlib import Path
from uuid import UUID

from sqlalchemy import text

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from app.db.session import engine
from app.repositories.chat_request_repository import (
    ChatRequestRepository,
)


USER_ID = UUID("e82dde04-5dd5-4935-8c73-1881bc20b056")

CONVERSATION_ID = UUID(
    "f6416a8d-47ab-4688-94d4-fa7cfedcae2a"
)

IDEMPOTENCY_KEY = UUID(
    "1df2bbdc-1044-488e-8b7a-2339d869fc82"
)


def main():
    repository = ChatRequestRepository()

    # Step 1: Confirm Worker A owns the request.
    existing = repository.get_request(
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        idempotency_key=IDEMPOTENCY_KEY,
    )

    if existing is None:
        raise RuntimeError("Request not found.")

    print("Current worker:", existing["worker_id"])
    print("Current attempt:", existing["attempt_count"])

    # Step 2: Simulate expiration by setting lease expiry
    # to a past timestamp. This does not delete any record.
    with engine.begin() as connection:
        connection.execute(
            text("""
                UPDATE public.pa_chat_requests
                SET lease_expires_at = now() - INTERVAL '1 second',
                    updated_at = now()
                WHERE user_id = :user_id
                  AND conversation_id = :conversation_id
                  AND idempotency_key = :idempotency_key
                  AND status = 'processing'
                  AND worker_id = 'worker-A'
            """),
            {
                "user_id": USER_ID,
                "conversation_id": CONVERSATION_ID,
                "idempotency_key": IDEMPOTENCY_KEY,
            },
        )

    # Step 3: Worker B attempts to reclaim.
    worker_b = repository.claim_request(
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        idempotency_key=IDEMPOTENCY_KEY,
        worker_id="worker-B",
        lease_seconds=120,
    )

    print("\nWorker B recovery claim:", worker_b is not None)

    if worker_b is None:
        raise RuntimeError("Worker B could not reclaim expired lease.")

    print("New worker:", worker_b["worker_id"])
    print("New attempt count:", worker_b["attempt_count"])

    print("\nRecovery verification completed.")


if __name__ == "__main__":
    main()
