
import sys
from pathlib import Path
from uuid import UUID, uuid4

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


def main():
    repository = ChatRequestRepository()
    idempotency_key = uuid4()

    # 1. Create request.
    record, created = repository.create_request(
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        idempotency_key=idempotency_key,
    )

    print("Request created:", created)

    # 2. Worker A claims.
    worker_a = repository.claim_request(
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        idempotency_key=idempotency_key,
        worker_id="worker-A",
        lease_seconds=120,
    )

    print("Worker A claimed:", worker_a is not None)

    if worker_a is None:
        raise RuntimeError("Worker A claim failed.")

    # 3. Simulate expired lease.
    with engine.begin() as connection:
        connection.execute(
            text("""
                UPDATE public.pa_chat_requests
                SET lease_expires_at = now() - INTERVAL '1 second'
                WHERE user_id = :user_id
                  AND conversation_id = :conversation_id
                  AND idempotency_key = :idempotency_key
                  AND worker_id = 'worker-A'
                  AND status = 'processing'
            """),
            {
                "user_id": USER_ID,
                "conversation_id": CONVERSATION_ID,
                "idempotency_key": idempotency_key,
            },
        )

    # 4. Worker B reclaims.
    worker_b = repository.claim_request(
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        idempotency_key=idempotency_key,
        worker_id="worker-B",
        lease_seconds=120,
    )

    print("Worker B reclaimed:", worker_b is not None)

    if worker_b is None:
        raise RuntimeError("Worker B reclaim failed.")

    # 5. Old worker attempts completion.
    stale_update = repository.update_status(
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        idempotency_key=idempotency_key,
        worker_id="worker-A",
        status="completed",
    )

    print(
        "Worker A completion rejected:",
        stale_update is None,
    )

    # 6. Current worker completes.
    valid_update = repository.update_status(
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        idempotency_key=idempotency_key,
        worker_id="worker-B",
        status="completed",
    )

    print(
        "Worker B completion accepted:",
        valid_update is not None,
    )

    print(
        "Final status:",
        valid_update["status"] if valid_update else None,
    )


if __name__ == "__main__":
    main()
