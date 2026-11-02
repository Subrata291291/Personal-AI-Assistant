import sys
from pathlib import Path

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1])
)

import hashlib
from uuid import UUID, uuid4

from app.repositories.chat_request_repository import (
    ChatRequestRepository,
    IdempotencyConflictError,
)


USER_ID = UUID("e82dde04-5dd5-4935-8c73-1881bc20b056")

CONVERSATION_ID = UUID(
    "f6416a8d-47ab-4688-94d4-fa7cfedcae2a"
)

repository = ChatRequestRepository()

idempotency_key = uuid4()

original_content = "Verify idempotency hash protection."
different_content = "This is a different request."

original_hash = hashlib.sha256(
    original_content.encode("utf-8")
).hexdigest()

different_hash = hashlib.sha256(
    different_content.encode("utf-8")
).hexdigest()


# 1. Create a new request
request, created = repository.create_request(
    user_id=USER_ID,
    conversation_id=CONVERSATION_ID,
    idempotency_key=idempotency_key,
    request_hash=original_hash,
)

assert created is True
assert request["request_hash"] == original_hash

print("New request creation: PASS")


# 2. Same key + same hash
duplicate, duplicate_created = repository.create_request(
    user_id=USER_ID,
    conversation_id=CONVERSATION_ID,
    idempotency_key=idempotency_key,
    request_hash=original_hash,
)

assert duplicate_created is False
assert duplicate["id"] == request["id"]

print("Same key + same hash: PASS")


# 3. Same key + different hash
try:
    repository.create_request(
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        idempotency_key=idempotency_key,
        request_hash=different_hash,
    )
except IdempotencyConflictError:
    print("Same key + different hash: PASS")
else:
    raise AssertionError(
        "Different payload was incorrectly accepted."
    )


# 4. Claim request
worker_id = f"verification-{uuid4()}"

claimed = repository.claim_request(
    user_id=USER_ID,
    conversation_id=CONVERSATION_ID,
    idempotency_key=idempotency_key,
    worker_id=worker_id,
)

assert claimed is not None
assert claimed["worker_id"] == worker_id
assert claimed["attempt_count"] == 1

print("Worker claim: PASS")


# 5. Complete the verification request as failed
# This is only a repository verification record.
failed = repository.update_status(
    user_id=USER_ID,
    conversation_id=CONVERSATION_ID,
    idempotency_key=idempotency_key,
    worker_id=worker_id,
    status="failed",
    error_message="Manual repository verification completed.",
)

assert failed is not None
assert failed["status"] == "failed"

print("Test request finalization: PASS")
print("All repository verification checks passed.")
