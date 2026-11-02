
import sys
from pathlib import Path

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1])
)

import hashlib
import time
from uuid import UUID, uuid4

from sqlalchemy import text

from app.db.session import engine
from app.repositories.chat_request_repository import (
    ChatRequestRepository,
)


USER_ID = UUID("e82dde04-5dd5-4935-8c73-1881bc20b056")

CONVERSATION_ID = UUID(
    "f6416a8d-47ab-4688-94d4-fa7cfedcae2a"
)

repository = ChatRequestRepository()

idempotency_key = uuid4()
request_hash = hashlib.sha256(
    f"execution-safety-{uuid4()}".encode("utf-8")
).hexdigest()

worker_a = f"worker-A-{uuid4()}"
worker_b = f"worker-B-{uuid4()}"
worker_c = f"worker-C-{uuid4()}"

request = None


try:
    # Find an existing message in this conversation.
    # We will reference it only for this verification record.
    with engine.connect() as connection:
        message_id = connection.execute(
            text("""
                SELECT id
                FROM public.pa_conversation_messages
                WHERE conversation_id = :conversation_id
                ORDER BY created_at DESC
                LIMIT 1
            """),
            {"conversation_id": CONVERSATION_ID},
        ).scalar_one_or_none()

    if message_id is None:
        raise RuntimeError(
            "No existing message found in the verification conversation."
        )

    # 1. Create a fresh request.
    request, created = repository.create_request(
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
    )

    assert created is True
    print("Request creation: PASS")

    # 2. Worker A claims a short lease.
    claimed_a = repository.claim_request(
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        idempotency_key=idempotency_key,
        worker_id=worker_a,
        lease_seconds=1,
    )

    assert claimed_a is not None
    print("Worker A initial claim: PASS")

    # 3. Let the lease expire before execution starts.
    time.sleep(2)

    # 4. Worker B reclaims the expired request.
    claimed_b = repository.claim_request(
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        idempotency_key=idempotency_key,
        worker_id=worker_b,
        lease_seconds=60,
    )

    assert claimed_b is not None
    assert claimed_b["worker_id"] == worker_b
    assert claimed_b["attempt_count"] == 2

    print("Pre-execution lease recovery: PASS")

    # 5. Attach an existing message to this test request.
    attached = repository.attach_user_message(
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        idempotency_key=idempotency_key,
        worker_id=worker_b,
        user_message_id=message_id,
    )

    assert attached is not None
    print("User message attachment: PASS")

    # 6. Mark execution as started.
    started = repository.mark_execution_started(
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        idempotency_key=idempotency_key,
        worker_id=worker_b,
    )

    assert started is not None
    assert started["execution_started_at"] is not None

    print("Execution start marking: PASS")

    # 7. Simulate an expired lease after execution started.
    with engine.begin() as connection:
        connection.execute(
            text("""
                UPDATE public.pa_chat_requests
                SET
                    lease_expires_at = now() - interval '1 second',
                    updated_at = now()
                WHERE id = :request_id
                  AND worker_id = :worker_id
                  AND status = 'processing'
            """),
            {
                "request_id": request["id"],
                "worker_id": worker_b,
            },
        )

    # 8. Worker C must NOT reclaim an execution-started request.
    claimed_c = repository.claim_request(
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        idempotency_key=idempotency_key,
        worker_id=worker_c,
        lease_seconds=60,
    )

    assert claimed_c is None

    print("Post-execution reclaim blocked: PASS")

finally:
    # Clean up only the new verification record.
    if request is not None:
        with engine.begin() as connection:
            connection.execute(
                text("""
                    UPDATE public.pa_chat_requests
                    SET
                        status = 'failed',
                        error_message = 'Execution safety verification completed.',
                        worker_id = NULL,
                        lease_expires_at = NULL,
                        updated_at = now()
                    WHERE id = :request_id
                      AND status = 'processing'
                """),
                {"request_id": request["id"]},
            )

        print("Verification record cleanup: PASS")

print("Execution safety verification finished.")
