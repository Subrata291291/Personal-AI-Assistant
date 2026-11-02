
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

from sqlalchemy import text

from app.db.session import engine
from app.repositories.chat_request_repository import ChatRequestRepository


def main():
    repository = ChatRequestRepository()

    # Find an existing conversation without modifying it.
    with engine.connect() as connection:
        conversation = (
            connection.execute(
                text("""
                    SELECT id, user_id
                    FROM public.pa_conversations
                    ORDER BY created_at DESC
                    LIMIT 1
                """)
            )
            .mappings()
            .one_or_none()
        )

    if conversation is None:
        print("STOP: No existing conversation found.")
        return

    user_id = conversation["user_id"]
    conversation_id = conversation["id"]

    idempotency_key = uuid4()

    worker_ids = [
        f"concurrency-worker-{uuid4()}",
        f"concurrency-worker-{uuid4()}",
    ]

    request_hash = hashlib.sha256(
        json.dumps(
            {
                "conversation_id": str(conversation_id),
                "content": "Concurrent worker verification",
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    created_request = False
    winner_worker_id = None

    try:
        # 1. Create a unique verification request.
        request, created = repository.create_request(
            user_id=user_id,
            conversation_id=conversation_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )

        if not created:
            raise RuntimeError("Unexpected idempotency-key collision.")

        created_request = True

        print("Verification request creation: PASS")

        # 2. Synchronize two workers before claiming.
        barrier = Barrier(2)

        def attempt_claim(worker_id):
            barrier.wait(timeout=10)

            return repository.claim_request(
                user_id=user_id,
                conversation_id=conversation_id,
                idempotency_key=idempotency_key,
                worker_id=worker_id,
                lease_seconds=600,
            )

        # 3. Attempt two concurrent claims.
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(attempt_claim, worker_id)
                for worker_id in worker_ids
            ]

            results = [
                future.result(timeout=30)
                for future in futures
            ]

        successful_claims = [
            (worker_id, result)
            for worker_id, result in zip(worker_ids, results)
            if result is not None
        ]

        if len(successful_claims) != 1:
            raise RuntimeError(
                "Expected exactly one successful worker claim; "
                f"received {len(successful_claims)}."
            )

        winner_worker_id = successful_claims[0][0]

        print("Concurrent claim: PASS")
        print("Exactly one worker acquired the lease: PASS")

        # 4. Verify the database owner.
        latest_request = repository.get_request(
            user_id=user_id,
            conversation_id=conversation_id,
            idempotency_key=idempotency_key,
        )

        if (
            latest_request is None
            or latest_request.get("worker_id") != winner_worker_id
        ):
            raise RuntimeError("Database worker ownership mismatch.")

        print("Worker ownership verification: PASS")

        # 5. Verify that the losing worker cannot renew the lease.
        losing_worker_id = next(
            worker_id
            for worker_id in worker_ids
            if worker_id != winner_worker_id
        )

        losing_renewal = repository.renew_lease(
            user_id=user_id,
            conversation_id=conversation_id,
            idempotency_key=idempotency_key,
            worker_id=losing_worker_id,
            lease_seconds=600,
        )

        if losing_renewal is not None:
            raise RuntimeError("Losing worker renewed the lease.")

        print("Losing worker renewal rejection: PASS")

        print("\nAll concurrency verification checks passed.")

    finally:
        # Finalize only this script's own verification request.
        # No existing application records are deleted.
        if created_request and winner_worker_id:
            finalized = repository.update_status(
                user_id=user_id,
                conversation_id=conversation_id,
                idempotency_key=idempotency_key,
                worker_id=winner_worker_id,
                status="failed",
                error_message="Concurrency verification completed.",
            )

            if finalized is not None:
                print("Verification record finalized: PASS")
            else:
                print(
                    "Verification record remains unchanged; "
                    "worker no longer owns an active lease."
                )


if __name__ == "__main__":
    main()
