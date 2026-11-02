
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
        f"idempotency-worker-{uuid4()}",
        f"idempotency-worker-{uuid4()}",
    ]

    content = "Concurrent idempotency verification"

    request_hash = hashlib.sha256(
        json.dumps(
            {
                "conversation_id": str(conversation_id),
                "content": content,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    barrier = Barrier(2)
    created_request = False
    winner_worker_id = None

    try:
        # 1. Both workers attempt request creation concurrently.
        def create_request(worker_id):
            barrier.wait(timeout=10)

            return repository.create_request(
                user_id=user_id,
                conversation_id=conversation_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(create_request, worker_id)
                for worker_id in worker_ids
            ]

            results = [
                future.result(timeout=30)
                for future in futures
            ]

        records = [result[0] for result in results]
        creation_flags = [result[1] for result in results]

        if sum(creation_flags) != 1:
            raise RuntimeError(
                "Expected exactly one newly created request."
            )

        created_request = True

        if str(records[0]["id"]) != str(records[1]["id"]):
            raise RuntimeError(
                "Concurrent requests received different request IDs."
            )

        print("Concurrent request creation: PASS")
        print("Exactly one database request created: PASS")
        print("Both workers received the same request ID: PASS")

        request_id = records[0]["id"]

        # 2. Both workers attempt to claim the same request.
        claim_barrier = Barrier(2)

        def claim_request(worker_id):
            claim_barrier.wait(timeout=10)

            return repository.claim_request(
                user_id=user_id,
                conversation_id=conversation_id,
                idempotency_key=idempotency_key,
                worker_id=worker_id,
                lease_seconds=600,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(claim_request, worker_id)
                for worker_id in worker_ids
            ]

            claim_results = [
                future.result(timeout=30)
                for future in futures
            ]

        successful_claims = [
            (worker_id, result)
            for worker_id, result in zip(worker_ids, claim_results)
            if result is not None
        ]

        if len(successful_claims) != 1:
            raise RuntimeError(
                "Expected exactly one successful worker claim."
            )

        winner_worker_id = successful_claims[0][0]

        print("Concurrent worker claim: PASS")
        print("Duplicate worker claim rejected: PASS")

        # 3. Verify the final database record.
        latest_request = repository.get_request(
            user_id=user_id,
            conversation_id=conversation_id,
            idempotency_key=idempotency_key,
        )

        if (
            latest_request is None
            or str(latest_request["id"]) != str(request_id)
            or latest_request.get("worker_id") != winner_worker_id
        ):
            raise RuntimeError(
                "Final request ownership verification failed."
            )

        print("Final request ownership: PASS")

        print("\nAll concurrent idempotency checks passed.")

    finally:
        # Finalize only this script's own verification request.
        # Existing application records are never deleted.
        if created_request:
            if winner_worker_id:
                finalized = repository.update_status(
                    user_id=user_id,
                    conversation_id=conversation_id,
                    idempotency_key=idempotency_key,
                    worker_id=winner_worker_id,
                    status="failed",
                    error_message="Concurrent idempotency verification completed.",
                )

                if finalized is not None:
                    print("Verification record finalized: PASS")
                else:
                    print("Verification record not finalized; ownership changed.")

            else:
                # No worker claimed the request.
                with engine.begin() as connection:
                    connection.execute(
                        text("""
                            UPDATE public.pa_chat_requests
                            SET
                                status = 'failed',
                                error_message = 'Concurrent idempotency verification completed.',
                                worker_id = NULL,
                                lease_expires_at = NULL,
                                updated_at = now()
                            WHERE user_id = :user_id
                              AND conversation_id = :conversation_id
                              AND idempotency_key = :idempotency_key
                              AND status = 'processing'
                              AND worker_id IS NULL
                        """),
                        {
                            "user_id": user_id,
                            "conversation_id": conversation_id,
                            "idempotency_key": idempotency_key,
                        },
                    )

                print("Verification record finalized without deletion.")


if __name__ == "__main__":
    main()
