
import hashlib
import json
import re
from threading import Event, Thread
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo
from app.core.error_logging import log_exception_traceback
from app.memory.memory_pipeline import MemoryPipeline
from app.services.user_settings_service import UserSettingsService
import logging

logger = logging.getLogger(__name__)
from app.repositories.chat_request_repository import (
    ChatRequestRepository,
)
from app.services.message_service import MessageService
from app.workflows.chat_graph import chat_graph
from app.services import message_service


class ChatRequestInProgressError(Exception):
    """Raised when another worker owns an active request."""


class ChatRequestUncertainError(Exception):
    """Raised when execution started but its outcome is uncertain."""


class ChatRequestFailedError(Exception):
    """Raised when the same request previously failed."""


class ChatService:

    LEASE_SECONDS = 600
    HEARTBEAT_INTERVAL = 60
    HEARTBEAT_JOIN_TIMEOUT = 10
    GMAIL_MESSAGE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,255}$")

    def __init__(
        self,
        message_service: MessageService,
        user_settings_service: UserSettingsService,
        memory_pipeline: MemoryPipeline,
        request_repository: ChatRequestRepository | None = None,
    ):
        self.message_service = message_service
        self.user_settings_service = user_settings_service
        self.memory_pipeline = memory_pipeline

        self.request_repository = (
            request_repository or ChatRequestRepository()
        )

    @staticmethod
    def _generate_request_hash(
        conversation_id: UUID,
        content: str,
        gmail_connection_id: UUID | None = None,
        user_timezone: str = "UTC",
    ) -> str:
        """Generate a deterministic SHA-256 fingerprint."""

        payload = {
            "conversation_id": str(conversation_id),
            "content": content.strip(),
        }
        if user_timezone != "UTC":
            payload["user_timezone"] = user_timezone
        if gmail_connection_id is not None:
            payload["gmail_connection_id"] = str(gmail_connection_id)

        canonical_payload = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        )

        return hashlib.sha256(
            canonical_payload.encode("utf-8")
        ).hexdigest()

    @classmethod
    def _available_gmail_sources(
        cls,
        history: list[dict[str, Any]],
        connection_id: UUID | None,
    ) -> dict[str, Any] | None:
        if connection_id is None:
            return None

        for message in reversed(history):
            if message.get("role") != "assistant":
                continue
            metadata = message.get("metadata")
            sources = metadata.get("gmail_sources") if isinstance(metadata, dict) else None
            if (
                not isinstance(sources, dict)
                or sources.get("connection_id") != str(connection_id)
                or not isinstance(sources.get("messages"), list)
            ):
                continue

            safe_messages = []
            for source in sources["messages"][:10]:
                if not isinstance(source, dict):
                    continue
                message_id = source.get("message_id", "")
                thread_id = source.get("thread_id", "")
                if (
                    not isinstance(message_id, str)
                    or not isinstance(thread_id, str)
                    or (
                        not cls.GMAIL_MESSAGE_ID_PATTERN.fullmatch(message_id)
                        and not cls.GMAIL_MESSAGE_ID_PATTERN.fullmatch(thread_id)
                    )
                ):
                    continue
                safe_messages.append({
                    "message_id": message_id,
                    "thread_id": thread_id,
                    "sender": str(source.get("sender") or "")[:320],
                    "recipient": str(source.get("recipient") or "")[:500],
                    "cc": str(source.get("cc") or "")[:500],
                    "subject": str(source.get("subject") or "")[:500],
                    "date": str(source.get("date") or "")[:100],
                })
            if safe_messages:
                return {
                    "connection_id": str(connection_id),
                    "messages": safe_messages,
                }
        return None

    @staticmethod
    def _has_available_email_draft(
        history: list[dict[str, Any]],
    ) -> bool:
        return any(
            message.get("role") == "assistant"
            and isinstance(message.get("metadata"), dict)
            and isinstance(
                message["metadata"].get("email_draft"),
                dict,
            )
            for message in history
        )

    def _get_completed_response(
        self,
        user_id: UUID,
        conversation_id: UUID,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        """Reconstruct the original response from saved messages."""

        user_message_id = request.get("user_message_id")
        assistant_message_id = request.get("assistant_message_id")

        if not user_message_id or not assistant_message_id:
            raise RuntimeError(
                "Completed request is missing message references."
            )

        user_message = self.message_service.get_message(
            user_id=user_id,
            conversation_id=conversation_id,
            message_id=UUID(str(user_message_id)),
        )

        assistant_message = self.message_service.get_message(
            user_id=user_id,
            conversation_id=conversation_id,
            message_id=UUID(str(assistant_message_id)),
        )

        if not user_message or not assistant_message:
            raise RuntimeError(
                "Saved messages for the completed request "
                "could not be retrieved."
            )

        metadata = assistant_message.get("metadata") or {}

        return {
            "conversation_id": conversation_id,
            "user_message": user_message,
            "assistant_message": assistant_message,
            "provider": metadata.get("provider", ""),
            "model": metadata.get("model", ""),
        }

    def _get_or_create_user_message(
        self,
        user_id: UUID,
        conversation_id: UUID,
        request_id: UUID,
        idempotency_key: UUID,
        worker_id: str,
        content: str,
    ) -> dict[str, Any]:
        """Recover an existing request-linked user message."""

        existing_message = self.message_service.get_request_message(
            user_id=user_id,
            conversation_id=conversation_id,
            chat_request_id=request_id,
            role="user",
        )

        if existing_message is None:
            try:
                existing_message = (
                    self.message_service.create_request_message(
                        conversation_id=conversation_id,
                        chat_request_id=request_id,
                        role="user",
                        content=content,
                    )
                )
            except Exception:
                # A concurrent insert may have won the unique index.
                existing_message = (
                    self.message_service.get_request_message(
                        user_id=user_id,
                        conversation_id=conversation_id,
                        chat_request_id=request_id,
                        role="user",
                    )
                )

                if existing_message is None:
                    raise

        attached = self.request_repository.attach_user_message(
            user_id=user_id,
            conversation_id=conversation_id,
            idempotency_key=idempotency_key,
            worker_id=worker_id,
            user_message_id=UUID(str(existing_message["id"])),
        )

        if attached is None:
            current_request = self.request_repository.get_request(
                user_id=user_id,
                conversation_id=conversation_id,
                idempotency_key=idempotency_key,
            )

            if (
                current_request is None
                or str(current_request.get("user_message_id"))
                != str(existing_message["id"])
                or current_request.get("worker_id") != worker_id
            ):
                raise ChatRequestUncertainError(
                    "The request lease was lost while attaching "
                    "the user message."
                )

        return existing_message

    @staticmethod
    def _heartbeat_loop(
        repository: ChatRequestRepository,
        user_id: UUID,
        conversation_id: UUID,
        idempotency_key: UUID,
        worker_id: str,
        stop_event: Event,
        lease_lost_event: Event,
        lease_seconds: int = 600,
        heartbeat_interval: int = 60,
    ) -> None:
        """Periodically renew the active worker lease."""

        while not stop_event.wait(heartbeat_interval):
            try:
                renewed = repository.renew_lease(
                    user_id=user_id,
                    conversation_id=conversation_id,
                    idempotency_key=idempotency_key,
                    worker_id=worker_id,
                    lease_seconds=lease_seconds,
                )

                if renewed is None:
                    lease_lost_event.set()
                    return

            except Exception:
                # Ownership cannot be confirmed.
                lease_lost_event.set()
                return

    def _start_heartbeat(
        self,
        user_id: UUID,
        conversation_id: UUID,
        idempotency_key: UUID,
        worker_id: str,
    ) -> tuple[Event, Event, Thread]:
        """Start the background lease-renewal thread."""

        stop_event = Event()
        lease_lost_event = Event()

        heartbeat_thread = Thread(
            target=self._heartbeat_loop,
            kwargs={
                "repository": self.request_repository,
                "user_id": user_id,
                "conversation_id": conversation_id,
                "idempotency_key": idempotency_key,
                "worker_id": worker_id,
                "stop_event": stop_event,
                "lease_lost_event": lease_lost_event,
                "lease_seconds": self.LEASE_SECONDS,
                "heartbeat_interval": self.HEARTBEAT_INTERVAL,
            },
            daemon=True,
            name=f"heartbeat-{worker_id}",
        )

        heartbeat_thread.start()

        return stop_event, lease_lost_event, heartbeat_thread

    def _stop_heartbeat(
        self,
        stop_event: Event,
        lease_lost_event: Event,
        heartbeat_thread: Thread,
    ) -> None:
        """Stop heartbeat and verify that the thread has terminated."""

        stop_event.set()
        heartbeat_thread.join(
            timeout=self.HEARTBEAT_JOIN_TIMEOUT
        )

        if heartbeat_thread.is_alive():
            raise ChatRequestUncertainError(
                "Heartbeat thread did not stop safely."
            )

        if lease_lost_event.is_set():
            raise ChatRequestUncertainError(
                "Worker lease ownership could not be confirmed."
            )

    def send_message(
        self,
        user_id: UUID,
        conversation_id: UUID,
        content: str,
        idempotency_key: UUID,
        gmail_connection_id: UUID | None = None,
        user_timezone: str = "UTC",
    ) -> dict[str, Any]:
        """Process an idempotent chat request using LangGraph."""

        if not content or not content.strip():
            raise ValueError("Message content cannot be empty.")

        normalized_content = content.strip()
        try:
            ZoneInfo(user_timezone)
        except (ValueError, KeyError) as exc:
            raise ValueError("A valid IANA timezone is required.") from exc

        # 1. Verify conversation ownership.
        if not self.message_service.verify_conversation_ownership(
            user_id=user_id,
            conversation_id=conversation_id,
        ):
            raise LookupError("Conversation not found.")

        # 2. Generate request fingerprint.
        request_hash = self._generate_request_hash(
            conversation_id=conversation_id,
            content=normalized_content,
            gmail_connection_id=gmail_connection_id,
            user_timezone=user_timezone,
        )

        # 3. Create or retrieve the idempotent request.
        request, _created = self.request_repository.create_request(
            user_id=user_id,
            conversation_id=conversation_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )

        # 4. Return the previous response if already completed.
        if request["status"] == "completed":
            return self._get_completed_response(
                user_id=user_id,
                conversation_id=conversation_id,
                request=request,
            )

        # 5. Recover an assistant message saved before a crash.
        if (
            request["status"] == "processing"
            and request.get("execution_started_at") is not None
            and request.get("user_message_id") is not None
            and request.get("assistant_message_id") is None
        ):
            existing_assistant = (
                self.message_service.get_request_message(
                    user_id=user_id,
                    conversation_id=conversation_id,
                    chat_request_id=request["id"],
                    role="assistant",
                )
            )

            if existing_assistant is not None:
                reconciled = (
                    self.request_repository.reconcile_completed_request(
                        user_id=user_id,
                        conversation_id=conversation_id,
                        idempotency_key=idempotency_key,
                        user_message_id=request["user_message_id"],
                        assistant_message_id=existing_assistant["id"],
                    )
                )

                if reconciled is not None:
                    return self._get_completed_response(
                        user_id=user_id,
                        conversation_id=conversation_id,
                        request=reconciled,
                    )

                latest_request = self.request_repository.get_request(
                    user_id=user_id,
                    conversation_id=conversation_id,
                    idempotency_key=idempotency_key,
                )

                if (
                    latest_request
                    and latest_request["status"] == "completed"
                ):
                    return self._get_completed_response(
                        user_id=user_id,
                        conversation_id=conversation_id,
                        request=latest_request,
                    )

        # 6. Never automatically restart failed requests.
        if request["status"] == "failed":
            raise ChatRequestFailedError(
                "This chat request previously failed. "
                "Use a new Idempotency-Key for a new attempt."
            )

        if request["status"] != "processing":
            raise RuntimeError("Unknown chat request status.")

        # 7. Claim the request.
        worker_id = f"chat-worker-{uuid4()}"

        claimed_request = self.request_repository.claim_request(
            user_id=user_id,
            conversation_id=conversation_id,
            idempotency_key=idempotency_key,
            worker_id=worker_id,
            lease_seconds=self.LEASE_SECONDS,
        )

        if claimed_request is None:
            current_request = self.request_repository.get_request(
                user_id=user_id,
                conversation_id=conversation_id,
                idempotency_key=idempotency_key,
            )

            if (
                current_request
                and current_request.get("execution_started_at")
            ):
                raise ChatRequestUncertainError(
                    "Execution has already started. "
                    "Automatic re-execution is blocked."
                )

            raise ChatRequestInProgressError(
                "This chat request is currently being processed."
            )

        request_id = UUID(str(claimed_request["id"]))

        heartbeat_stop_event = None
        heartbeat_lease_lost_event = None
        heartbeat_thread = None

        try:
            # 8. Recover or create the request-linked user message.
            user_message = self._get_or_create_user_message(
                user_id=user_id,
                conversation_id=conversation_id,
                request_id=request_id,
                idempotency_key=idempotency_key,
                worker_id=worker_id,
                content=normalized_content,
            )

            # Read the user's automatic memory preference.
            user_settings = self.user_settings_service.get_settings(
                user_id=user_id,
            )

            memory_enabled = user_settings.memory_enabled

            # 9. Load history before marking execution started.
            history = self.message_service.get_conversation_messages(
                user_id=user_id,
                conversation_id=conversation_id,
                limit=100,
            )
            available_gmail_sources = self._available_gmail_sources(
                history,
                gmail_connection_id,
            )

            previous_messages = [
                {
                    "role": message["role"],
                    "content": message["content"],
                }
                for message in history
                if message["id"] != user_message["id"]
                and message["role"] in {"user", "assistant"}
            ]

            # 10. Mark execution started.
            started = self.request_repository.mark_execution_started(
                user_id=user_id,
                conversation_id=conversation_id,
                idempotency_key=idempotency_key,
                worker_id=worker_id,
            )

            if started is None:
                raise ChatRequestUncertainError(
                    "Could not safely start LLM execution."
                )

            # 11. Prepare LangGraph state.
            initial_state = {
                "user_id": str(user_id),
                "conversation_id": str(conversation_id),
                "user_message": normalized_content,
                "assistant_response": "",
                "provider": "",
                "model": "",
                "messages": previous_messages,
                "user_timezone": user_timezone,
            }
            if available_gmail_sources:
                initial_state["available_gmail_sources"] = (
                    available_gmail_sources
                )
            if self._has_available_email_draft(history):
                initial_state["available_email_draft"] = True

            thread_config = {
                "configurable": {
                    "thread_id": str(uuid4()),
                }
            }

            # 12. Start heartbeat immediately before LLM execution.
            (
                heartbeat_stop_event,
                heartbeat_lease_lost_event,
                heartbeat_thread,
            ) = self._start_heartbeat(
                user_id=user_id,
                conversation_id=conversation_id,
                idempotency_key=idempotency_key,
                worker_id=worker_id,
            )

            # 13. Execute LangGraph.
            try:
                result = chat_graph.invoke(
                    initial_state,
                    config=thread_config,
                    context={
                        "user_id": str(user_id),
                        "gmail_connection_id": (
                            str(gmail_connection_id)
                            if gmail_connection_id
                            else None
                        ),
                        "user_timezone": user_timezone,
                    },
                )
            finally:
                # Always stop heartbeat when graph execution ends,
                # whether it succeeds or raises an exception.
                self._stop_heartbeat(
                    stop_event=heartbeat_stop_event,
                    lease_lost_event=heartbeat_lease_lost_event,
                    heartbeat_thread=heartbeat_thread,
                )

            assistant_content = result.get("assistant_response")

            if (
                not isinstance(assistant_content, str)
                or not assistant_content.strip()
            ):
                raise RuntimeError(
                    "The AI workflow returned an empty response."
                )
            # Save memory from the user's message only.
            # Existing memory retrieval remains enabled even when
            # automatic memory storage is disabled.

            try:
                memory_result = self.memory_pipeline.process(
                    user_id=user_id,
                    content=normalized_content,
                    memory_enabled=memory_enabled,
                    source_conversation_id=conversation_id,
                )

                logger.info(
                    "Memory pipeline completed: status=%s, user_id=%s",
                    memory_result.status,
                    user_id,
                )

            except Exception:
                logger.exception(
                    "Memory pipeline failed. Chat response will continue."
                )

            # 14. Recover or create the assistant message.
            assistant_message = (
                self.message_service.get_request_message(
                    user_id=user_id,
                    conversation_id=conversation_id,
                    chat_request_id=request_id,
                    role="assistant",
                )
            )

            if assistant_message is None:
                try:
                    assistant_metadata = {
                        "provider": result["provider"],
                        "model": result["model"],
                    }
                    if result.get("email_draft"):
                        assistant_metadata["email_draft"] = (
                            result["email_draft"]
                        )
                    if result.get("email_draft_connection_id"):
                        assistant_metadata["email_draft_connection_id"] = (
                            result["email_draft_connection_id"]
                        )
                    if result.get("gmail_sources"):
                        assistant_metadata["gmail_sources"] = (
                            result["gmail_sources"]
                        )
                    if result.get("response_metadata"):
                        assistant_metadata["response_completion"] = (
                            result["response_metadata"]
                        )
                    assistant_message = (
                        self.message_service.create_request_message(
                            conversation_id=conversation_id,
                            chat_request_id=request_id,
                            role="assistant",
                            content=assistant_content,
                            metadata=assistant_metadata,
                        )
                    )
                except Exception:
                    assistant_message = (
                        self.message_service.get_request_message(
                            user_id=user_id,
                            conversation_id=conversation_id,
                            chat_request_id=request_id,
                            role="assistant",
                        )
                    )

                    if assistant_message is None:
                        raise

            # 15. Complete request using worker fencing.
            completed = self.request_repository.update_status(
                user_id=user_id,
                conversation_id=conversation_id,
                idempotency_key=idempotency_key,
                worker_id=worker_id,
                status="completed",
                user_message_id=UUID(str(user_message["id"])),
                assistant_message_id=UUID(str(assistant_message["id"])),
            )

            if completed is None:
                raise ChatRequestUncertainError(
                    "The assistant response was saved, but the worker "
                    "could not finalize the request."
                )

            # 16. Return the response.
            metadata = assistant_message.get("metadata") or {}

            return {
                "conversation_id": conversation_id,
                "user_message": user_message,
                "assistant_message": assistant_message,
                "provider": metadata.get("provider", ""),
                "model": metadata.get("model", ""),
            }

        except ChatRequestUncertainError:
            raise

        except Exception:
            # Only the active worker can mark the request failed.
            try:
                self.request_repository.update_status(
                    user_id=user_id,
                    conversation_id=conversation_id,
                    idempotency_key=idempotency_key,
                    worker_id=worker_id,
                    status="failed",
                    error_message="Chat execution failed.",
                )
            except Exception as exc:
                log_exception_traceback(
                    logger,
                    "Could not persist the failed chat request status.",
                    exc,
                )
            raise
