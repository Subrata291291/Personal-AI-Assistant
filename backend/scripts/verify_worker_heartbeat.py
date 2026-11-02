
import hashlib
import json
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
        print("Create a conversation before running this verification.")
        return

    user_id = conversation["user_id"]
    conversation_id = conversation["id"]

    idempotency_key = uuid4()
    worker_id = f"heartbeat-test-{uuid4()}"

    request_hash = hashlib.sha256(
        json.dumps(
            {
                "conversation_id": str(conversation_id),
                "content": "Worker heartbeat verification",
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    request = None

    try:
        # 1. Create an isolated verification request.
        request, created = repository.create_request(
            user_id=user_id,
            conversation_id=conversation_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )

        if not created:
            raise RuntimeError("Unexpected idempotency-key collision.")

        print("Verification request creation: PASS")

        # 2. Claim the request.
        claimed = repository.claim_request(
            user_id=user_id,
            conversation_id=conversation_id,
            idempotency_key=idempotency_key,
            worker_id=worker_id,
            lease_seconds=600,
        )

        if claimed is None:
            raise RuntimeError("Could not claim verification request.")

        print("Worker claim: PASS")

        # 3. Correct worker should renew the active lease.
        renewed = repository.renew_lease(
            user_id=user_id,
            conversation_id=conversation_id,
            idempotency_key=idempotency_key,
            worker_id=worker_id,
            lease_seconds=600,
        )

        if renewed is None:
            raise RuntimeError("Active lease renewal failed.")

        print("Active lease renewal: PASS")

        # 4. Wrong worker must not renew the lease.
        wrong_worker_result = repository.renew_lease(
            user_id=user_id,
            conversation_id=conversation_id,
            idempotency_key=idempotency_key,
            worker_id="incorrect-worker",
            lease_seconds=600,
        )

        if wrong_worker_result is not None:
            raise RuntimeError("Wrong worker unexpectedly renewed lease.")

        print("Wrong worker rejection: PASS")

        # 5. Expire this verification lease explicitly.
        with engine.begin() as connection:
            connection.execute(
                text("""
                    UPDATE public.pa_chat_requests
                    SET lease_expires_at = now() - interval '1 second'
                    WHERE user_id = :user_id
                      AND conversation_id = :conversation_id
                      AND idempotency_key = :idempotency_key
                      AND worker_id = :worker_id
                      AND status = 'processing'
                """),
                {
                    "user_id": user_id,
                    "conversation_id": conversation_id,
                    "idempotency_key": idempotency_key,
                    "worker_id": worker_id,
                },
            )

        # 6. Expired lease must not be renewed.
        expired_result = repository.renew_lease(
            user_id=user_id,
            conversation_id=conversation_id,
            idempotency_key=idempotency_key,
            worker_id=worker_id,
            lease_seconds=600,
        )

        if expired_result is not None:
            raise RuntimeError("Expired lease was unexpectedly renewed.")

        print("Expired lease rejection: PASS")

        print("\nAll heartbeat verification checks passed.")

    finally:
        # Finalize only this script's own verification record.
        # No existing application records are deleted.
        if request is not None:
            with engine.begin() as connection:
                connection.execute(
                    text("""
                        UPDATE public.pa_chat_requests
                        SET
                            status = 'failed',
                            error_message = 'Heartbeat verification completed.',
                            worker_id = NULL,
                            lease_expires_at = NULL,
                            updated_at = now()
                        WHERE user_id = :user_id
                          AND conversation_id = :conversation_id
                          AND idempotency_key = :idempotency_key
                          AND status = 'processing'
                    """),
                    {
                        "user_id": user_id,
                        "conversation_id": conversation_id,
                        "idempotency_key": idempotency_key,
                    },
                )

            print("Verification record finalized; no records deleted.")


if __name__ == "__main__":
    main()
