
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.db.session import engine
from app.repositories.chat_request_repository import ChatRequestRepository


def concurrent_insert(
    conversation_id,
    request_id,
    role,
):
    barrier = concurrent_insert.barrier

    def insert_message():
        barrier.wait(timeout=10)

        try:
            with engine.begin() as connection:
                result = connection.execute(
                    text("""
                        INSERT INTO public.pa_conversation_messages (
                            conversation_id,
                            chat_request_id,
                            role,
                            content
                        )
                        VALUES (
                            :conversation_id,
                            :request_id,
                            :role,
                            :content
                        )
                        RETURNING id
                    """),
                    {
                        "conversation_id": conversation_id,
                        "request_id": request_id,
                        "role": role,
                        "content": f"Verification message for {role}",
                    },
                )

                return {
                    "status": "inserted",
                    "id": result.scalar_one(),
                }

        except IntegrityError:
            return {"status": "duplicate_rejected"}

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(insert_message),
            executor.submit(insert_message),
        ]

        return [future.result(timeout=30) for future in futures]


def verify_role(conversation_id, request_id, role):
    concurrent_insert.barrier = Barrier(2)

    results = concurrent_insert(
        conversation_id=conversation_id,
        request_id=request_id,
        role=role,
    )

    inserted = [
        item for item in results
        if item["status"] == "inserted"
    ]

    rejected = [
        item for item in results
        if item["status"] == "duplicate_rejected"
    ]

    if len(inserted) != 1 or len(rejected) != 1:
        raise RuntimeError(
            f"{role} duplicate protection failed: {results}"
        )

    print(f"Concurrent {role} message insert: PASS")
    print(f"Duplicate {role} message rejected: PASS")


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

    request_hash = hashlib.sha256(
        json.dumps(
            {
                "conversation_id": str(conversation_id),
                "content": "Duplicate message verification",
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    created_request = False
    worker_id = f"duplicate-message-worker-{uuid4()}"

    try:
        # 1. Create an isolated request.
        request, created = repository.create_request(
            user_id=user_id,
            conversation_id=conversation_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )

        if not created:
            raise RuntimeError("Unexpected idempotency-key collision.")

        created_request = True
        request_id = request["id"]

        print("Verification request creation: PASS")

        # 2. Concurrent user message inserts.
        verify_role(
            conversation_id,
            request_id,
            "user",
        )

        # 3. Concurrent assistant message inserts.
        verify_role(
            conversation_id,
            request_id,
            "assistant",
        )

        # 4. Verify exactly one message exists for each role.
        with engine.connect() as connection:
            counts = connection.execute(
                text("""
                    SELECT role, COUNT(*) AS message_count
                    FROM public.pa_conversation_messages
                    WHERE conversation_id = :conversation_id
                      AND chat_request_id = :request_id
                    GROUP BY role
                """),
                {
                    "conversation_id": conversation_id,
                    "request_id": request_id,
                },
            ).mappings().all()

        role_counts = {
            row["role"]: row["message_count"]
            for row in counts
        }

        if (
            role_counts.get("user") != 1
            or role_counts.get("assistant") != 1
        ):
            raise RuntimeError(
                f"Unexpected message counts: {role_counts}"
            )

        print("One user message per request: PASS")
        print("One assistant message per request: PASS")

        print("\nAll duplicate message protection checks passed.")

    finally:
        # Finalize only this script's own request.
        # Verification messages are retained; no records are deleted.
        if created_request:
            with engine.begin() as connection:
                connection.execute(
                    text("""
                        UPDATE public.pa_chat_requests
                        SET
                            status = 'failed',
                            error_message = 'Duplicate message verification completed.',
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

            print("Verification request finalized; no records deleted.")


if __name__ == "__main__":
    main()
