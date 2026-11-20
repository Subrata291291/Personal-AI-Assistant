
import hashlib
import json
import re
from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone
from threading import Event, Thread
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5
from zoneinfo import ZoneInfo
from app.core.error_logging import log_exception_traceback
from app.core.config import settings
from app.core.diagnostics import lifecycle_event
from app.core.request_metrics import current_request_metrics
from app.llm.streaming import streaming_mode
from app.memory.memory_pipeline import MemoryPipeline
from app.services.user_settings_service import UserSettingsService
from app.workflows.calendar_update_parser import (
    log_calendar_update_diagnostic,
)
import logging

logger = logging.getLogger(__name__)
from app.repositories.chat_request_repository import (
    ChatRequestRepository,
)
from app.services.message_service import MessageService
from app.services.user_connection_service import UserConnectionService
from app.workflows.chat_graph import ASSISTANT_ROUTE_NODES, chat_graph
from app.workflows.gmail_agent_nodes import (
    _extract_explicit_calendar_date,
    _relative_calendar_date,
    build_runtime_capabilities,
)
from app.services import message_service
from app.schemas.action_approval import ApprovalProposalCreate
from app.services.action_approval_service import ActionApprovalService


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
    CALENDAR_ACTION_CLARIFICATION_TTL_SECONDS = 30 * 60
    GMAIL_MESSAGE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,255}$")
    # Friendly labels for node starts observed through LangGraph debug events.
    # Only these nodes emit status events this way; nodes that produce their own
    # status text (Gmail, Calendar, Contacts, the LLM node) are omitted to avoid
    # duplicate messages. Debug payloads themselves are never forwarded.
    STREAM_NODE_STATUS = {
        "gmail_intent_router_node": "Understanding your request...",
        "memory_retrieval_node": "Checking saved memories...",
        "autonomous_memory_node": "Checking saved memories...",
    }
    STREAM_MAX_CONTENT_CHARS = 50000

    def __init__(
        self,
        message_service: MessageService,
        user_settings_service: UserSettingsService,
        memory_pipeline: MemoryPipeline,
        request_repository: ChatRequestRepository | None = None,
        user_connection_service: UserConnectionService | None = None,
    ):
        self.message_service = message_service
        self.user_settings_service = user_settings_service
        self.memory_pipeline = memory_pipeline
        self.user_connection_service = user_connection_service

        self.request_repository = (
            request_repository or ChatRequestRepository()
        )

    @staticmethod
    def _generate_request_hash(
        conversation_id: UUID,
        content: str,
        gmail_connection_id: UUID | None = None,
        user_timezone: str = "UTC",
        default_calendar_connection_id: str | None = None,
        default_contacts_connection_id: str | None = None,
        default_drive_connection_id: str | None = None,
        gmail_enabled: bool = True,
        calendar_enabled: bool = True,
        contacts_enabled: bool = True,
        drive_enabled: bool = True,
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
        if default_calendar_connection_id:
            payload["default_calendar_connection_id"] = (
                str(default_calendar_connection_id)
            )
        if default_contacts_connection_id:
            payload["default_contacts_connection_id"] = (
                str(default_contacts_connection_id)
            )
        if default_drive_connection_id:
            payload["default_drive_connection_id"] = (
                str(default_drive_connection_id)
            )
        if not gmail_enabled:
            payload["gmail_enabled"] = False
        if not calendar_enabled:
            payload["calendar_enabled"] = False
        if not contacts_enabled:
            payload["contacts_enabled"] = False
        if not drive_enabled:
            payload["drive_enabled"] = False

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

    @staticmethod
    def _pending_calendar_create_proposal(
        history: list[dict[str, Any]],
        user_id: UUID,
        conversation_id: UUID,
        now: datetime,
    ) -> dict[str, Any] | None:
        for message in reversed(history):
            if message.get("role") != "assistant":
                continue
            metadata = message.get("metadata")
            proposal = (
                metadata.get("action_approval")
                if isinstance(metadata, dict)
                else None
            )
            if not isinstance(proposal, dict) or not str(
                proposal.get("action_type", "")
            ).startswith("calendar."):
                continue
            if (
                proposal.get("action_type") != "calendar.create_event"
                or proposal.get("status") not in {"pending", "approved"}
                or proposal.get("user_id") != str(user_id)
                or proposal.get("conversation_id") != str(conversation_id)
            ):
                return None
            try:
                expires_at = datetime.fromisoformat(
                    str(proposal["expires_at"]).replace("Z", "+00:00")
                )
            except (KeyError, TypeError, ValueError):
                return None
            if (
                expires_at.tzinfo is None
                or expires_at.astimezone(timezone.utc)
                <= now.astimezone(timezone.utc)
            ):
                return None
            return proposal
        return None

    @staticmethod
    def _pending_calendar_create_draft(
        history: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        for message in reversed(history):
            if message.get("role") != "assistant":
                continue
            metadata = message.get("metadata")
            if not isinstance(metadata, dict):
                continue
            proposal = metadata.get("action_approval")
            if (
                isinstance(proposal, dict)
                and proposal.get("action_type") == "calendar.create_event"
            ):
                return None
            if "calendar_create_draft" not in metadata:
                continue
            draft = metadata.get("calendar_create_draft")
            return draft if isinstance(draft, dict) else None
        return None

    @staticmethod
    def _pending_contacts_clarification(
        history: list[dict[str, Any]],
        *,
        user_id: UUID,
        conversation_id: UUID,
        connection_id: UUID | str | None = None,
        now: datetime,
    ) -> dict[str, Any] | None:
        for message in reversed(history):
            if message.get("role") != "assistant":
                continue
            metadata = message.get("metadata")
            if not isinstance(metadata, dict):
                continue
            pending = metadata.get("pending_contacts_clarification")
            if pending is None:
                # explicit clear in latest assistant message means no active clarification
                if "pending_contacts_clarification" in metadata:
                    return None
                continue
            if not isinstance(pending, dict):
                return None
            if (
                pending.get("version") != 1
                or pending.get("type") != "contacts_read_clarification"
                or str(pending.get("user_id")) != str(user_id)
                or str(pending.get("conversation_id")) != str(conversation_id)
            ):
                return None
            pending_connection_id = pending.get("connection_id")
            if (
                pending_connection_id is not None
                and connection_id is not None
                and str(pending_connection_id) != str(connection_id)
            ):
                return None
            try:
                expires_at = datetime.fromisoformat(
                    str(pending.get("expires_at")).replace("Z", "+00:00")
                )
            except Exception:
                return None
            if (
                expires_at.tzinfo is None
                or expires_at.astimezone(timezone.utc)
                <= now.astimezone(timezone.utc)
            ):
                return None
            candidates = pending.get("candidates")
            if not isinstance(candidates, list) or not candidates:
                return None
            # Trusted candidates must contain server-verified resource names.
            valid = True
            for c in candidates:
                rn = c.get("resource_name") if isinstance(c, dict) else None
                if not isinstance(rn, str) or not rn.startswith("people/"):
                    valid = False
                    break
            if not valid:
                return None
            return pending
        return None

    @staticmethod
    def _pending_calendar_availability(
        history: list[dict[str, Any]],
        *,
        user_id: UUID,
        conversation_id: UUID,
        now: datetime,
        connection_id: str | None = None,
    ) -> dict[str, Any] | None:
        for message in reversed(history):
            if message.get("role") != "assistant":
                continue
            metadata = message.get("metadata")
            pending = (
                metadata.get("pending_calendar_availability")
                if isinstance(metadata, dict)
                else None
            )
            if not isinstance(pending, dict):
                # A later turn may suspend this state by omitting it. Keep
                # scanning so a valid, unexpired earlier state can be restored.
                continue
            if (
                pending.get("version") != 1
                or pending.get("operation") != "availability"
                or pending.get("user_id") != str(user_id)
                or pending.get("conversation_id") != str(conversation_id)
                or (
                    connection_id is not None
                    and pending.get("connection_id") != str(connection_id)
                )
            ):
                return None
            try:
                expires_at = datetime.fromisoformat(
                    str(pending["expires_at"]).replace("Z", "+00:00")
                )
            except (KeyError, TypeError, ValueError):
                return None
            if (
                expires_at.tzinfo is None
                or expires_at.astimezone(timezone.utc)
                <= now.astimezone(timezone.utc)
            ):
                return None
            return pending
        return None

    @staticmethod
    def _pending_calendar_delete_selection(
        history: list[dict[str, Any]],
        *,
        user_id: UUID,
        conversation_id: UUID,
        connection_id: str | None,
        now: datetime,
    ) -> dict[str, Any] | None:
        for message in reversed(history):
            if message.get("role") != "assistant":
                continue
            metadata = message.get("metadata")
            if not isinstance(metadata, dict):
                continue
            pending = metadata.get("pending_calendar_delete_selection")
            if pending is None:
                if "pending_calendar_delete_selection" in metadata:
                    return None
                continue
            if not isinstance(pending, dict):
                return None
            if (
                pending.get("version") != 1
                or pending.get("operation") != "delete_selection"
                or str(pending.get("user_id")) != str(user_id)
                or str(pending.get("conversation_id")) != str(conversation_id)
                or not connection_id
                or str(pending.get("connection_id")) != str(connection_id)
            ):
                return None
            try:
                expires_at = datetime.fromisoformat(
                    str(pending.get("expires_at")).replace("Z", "+00:00")
                )
            except Exception:
                return None
            if (
                expires_at.tzinfo is None
                or expires_at.astimezone(timezone.utc)
                <= now.astimezone(timezone.utc)
            ):
                return None
            candidates = pending.get("candidates")
            if not isinstance(candidates, list) or not candidates:
                return None
            valid = True
            for c in candidates:
                if not isinstance(c, dict):
                    valid = False
                    break
                event_id = c.get("event_id")
                cal_id = c.get("calendar_id")
                if not isinstance(event_id, str) or not event_id.strip():
                    valid = False
                    break
                if not isinstance(cal_id, str) or not cal_id.strip():
                    valid = False
                    break
            if not valid:
                return None
            return pending
        return None

    @staticmethod
    def _pending_calendar_read(
        history: list[dict[str, Any]],
        *,
        user_id: UUID,
        conversation_id: UUID,
        now: datetime,
    ) -> dict[str, Any] | None:
        for item in reversed(history):
            if item.get("role") != "assistant":
                continue
            metadata = item.get("metadata") or {}
            pending = metadata.get("pending_calendar_read")
            if pending is None:
                if "pending_calendar_read" in metadata:
                    return None
                continue
            if not isinstance(pending, dict):
                continue
            if pending.get("version") != 1 or pending.get("type") != "calendar_read_continuation":
                continue
            try:
                expires_at = datetime.fromisoformat(str(pending.get("expires_at")).replace("Z", "+00:00"))
            except Exception:
                continue
            if expires_at.tzinfo is None or expires_at.astimezone(timezone.utc) <= now:
                continue
            if str(pending.get("user_id")) != str(user_id) or str(pending.get("conversation_id")) != str(conversation_id):
                continue
            return pending
        return None

    def _hydrate_action_approval_metadata(
        self,
        history: list[dict[str, Any]],
        *,
        user_id: UUID,
        conversation_id: UUID,
    ) -> list[dict[str, Any]]:
        """Refresh assistant-message approval metadata from the source of truth.

        Approval execution/completion happens outside the chat request path. The
        stored conversation message metadata can therefore become stale. For
        follow-up turns (e.g. contextual delete of a just-created event), the
        workflow must see the latest trusted completed status/result while
        preserving user and conversation isolation.
        """
        if not history or self.user_connection_service is None:
            return history
        db = getattr(self.user_connection_service, "db", None)
        if db is None:
            return history

        approval_service = ActionApprovalService(db)
        hydrated: list[dict[str, Any]] = []
        cache: dict[str, dict[str, Any] | None] = {}
        for message in history:
            metadata = message.get("metadata")
            proposal = metadata.get("action_approval") if isinstance(metadata, dict) else None
            if not isinstance(proposal, dict):
                hydrated.append(message)
                continue
            raw_id = proposal.get("id") or proposal.get("proposal_id")
            if raw_id is None:
                hydrated.append(message)
                continue
            try:
                proposal_uuid = UUID(str(raw_id))
            except Exception:
                hydrated.append(message)
                continue
            cache_key = str(proposal_uuid)
            if cache_key not in cache:
                try:
                    latest = approval_service.get_proposal(user_id, proposal_uuid)
                    latest_data = latest.model_dump(mode="json")
                except Exception:
                    latest_data = None
                cache[cache_key] = latest_data
            latest_data = cache[cache_key]
            if not isinstance(latest_data, dict):
                hydrated.append(message)
                continue
            # Only merge proposals that belong to this conversation and user.
            if str(latest_data.get("conversation_id")) != str(conversation_id):
                hydrated.append(message)
                continue
            if str(latest_data.get("user_id")) != str(user_id):
                hydrated.append(message)
                continue
            merged_metadata = dict(metadata)
            merged_metadata["action_approval"] = latest_data
            hydrated.append({**message, "metadata": merged_metadata})
        return hydrated

    @staticmethod
    def _pending_gmail_clarification(
        history: list[dict[str, Any]],
        *,
        user_id: UUID,
        conversation_id: UUID,
        connection_id: UUID | None = None,
        now: datetime,
    ) -> dict[str, Any] | None:
        for item in reversed(history):
            if item.get("role") != "assistant":
                continue
            metadata = item.get("metadata") or {}
            pending = metadata.get("pending_gmail_clarification")
            if pending is None:
                if "pending_gmail_clarification" in metadata:
                    return None
                continue
            if not isinstance(pending, dict):
                continue
            if pending.get("version") != 1 or pending.get("type") not in {
                "gmail_date_range_clarification",
                "gmail_search_fallback_clarification",
            }:
                continue
            if (
                str(pending.get("user_id")) != str(user_id)
                or str(pending.get("conversation_id")) != str(conversation_id)
            ):
                continue
            pending_connection_id = pending.get("connection_id")
            if (
                pending_connection_id is not None
                and connection_id is not None
                and str(pending_connection_id) != str(connection_id)
            ):
                continue
            try:
                expires_at = datetime.fromisoformat(
                    str(pending.get("expires_at")).replace("Z", "+00:00")
                )
            except Exception:
                continue
            if expires_at.tzinfo is None or expires_at.astimezone(timezone.utc) <= now.astimezone(timezone.utc):
                continue
            return pending
        return None


    @staticmethod
    def _pending_calendar_action(
        history: list[dict[str, Any]],
        *,
        user_id: UUID,
        conversation_id: UUID,
        connection_id: str | None,
        now: datetime,
    ) -> dict[str, Any] | None:
        for message in reversed(history):
            if message.get("role") != "assistant":
                continue
            metadata = message.get("metadata")
            pending = (
                metadata.get("pending_calendar_action")
                if isinstance(metadata, dict)
                else None
            )
            if not isinstance(pending, dict):
                return None
            if (
                pending.get("user_id") != str(user_id)
                or pending.get("conversation_id") != str(conversation_id)
                or not connection_id
                or pending.get("connection_id") != str(connection_id)
                or pending.get("version") != 1
                or pending.get("operation") not in {"create", "update", "delete"}
            ):
                return None
            try:
                expires_at = datetime.fromisoformat(
                    str(pending["expires_at"]).replace("Z", "+00:00")
                )
            except (KeyError, TypeError, ValueError):
                return None
            if (
                expires_at.tzinfo is None
                or expires_at.astimezone(timezone.utc)
                <= now.astimezone(timezone.utc)
            ):
                return None
            original_request = pending.get("original_request")
            if (
                not isinstance(original_request, str)
                or not original_request.strip()
            ):
                return None
            return pending
        return None

    @staticmethod
    def _calendar_action_clarification(
        *,
        result: dict[str, Any],
        response: str,
        current_message: str,
        pending: dict[str, Any] | None,
        user_id: UUID,
        conversation_id: UUID,
        connection_id: str | None,
        now: datetime,
    ) -> dict[str, Any] | None:
        if (
            not connection_id
            or result.get("assistant_route") != "google_calendar"
        ):
            return None

        plan = result.get("gmail_plan")
        tool = plan.get("calendar_tool") if isinstance(plan, dict) else None
        operation = {
            "create_calendar_event_proposal": "create",
            "update_calendar_event_proposal": "update",
            "delete_calendar_event_proposal": "delete",
        }.get(tool)
        if operation is None:
            return None

        clarification = response.casefold()
        # Terminal "no events / no match" outcomes are not actionable follow-ups;
        # persisting them as pending calendar actions can leak stale state into
        # unrelated future requests.
        if re.search(
            r"\b(?:no events were found|couldn't find any events|couldn['’]?t find any events|"
            r"couldn't find an existing calendar event|couldn['’]?t find an existing calendar event|"
            r"no matching event was found)\b",
            clarification,
        ):
            return None
        if not re.search(
            r"\b(?:which|what|please\s+(?:specify|include|provide|clarify)|"
            r"multiple possible|couldn['’]?t find|didn['’]?t find|"
            r"couldn['’]?t uniquely resolve|couldn['’]?t safely verify)\b",
            clarification,
        ):
            return None

        continuing = (
            pending
            if result.get("calendar_action_request")
            and pending
            and pending.get("operation") == operation
            else None
        )
        original_request = (
            f"{continuing['original_request']}\n"
            f"Clarification: {current_message.strip()}"
            if continuing
            else current_message.strip()
        )
        arguments = plan.get("calendar_arguments")
        known_details: dict[str, Any] = {}
        if isinstance(arguments, dict):
            try:
                known_details = json.loads(
                    json.dumps(arguments, allow_nan=False)
                )
            except (TypeError, ValueError):
                known_details = {}
        resolved_details = result.get("calendar_action_details")
        if isinstance(resolved_details, dict):
            known_details.update({
                key: value
                for key, value in resolved_details.items()
                if key in {"calendar_id", "event_query"}
                and isinstance(value, str)
            })

        draft = result.get("calendar_create_draft")
        if not isinstance(draft, dict) and continuing:
            draft = continuing.get("calendar_create_draft")
        if isinstance(draft, dict):
            try:
                draft = json.loads(json.dumps(draft, allow_nan=False))
            except (TypeError, ValueError):
                draft = None

        unresolved_fields = []
        for field, pattern in (
            ("event_identity", r"\bevent\b|\btitle\b"),
            ("date", r"\bdate\b|\btomorrow\b|\btoday\b"),
            ("time", r"\btime\b|\bstart\b|\bend\b"),
            ("requested_changes", r"\bchange\b|\bupdate\b|\bwhat would"),
            ("calendar", r"\bcalendar\b"),
        ):
            if re.search(pattern, clarification):
                unresolved_fields.append(field)

        calendar_id = known_details.get("calendar_id")
        if not isinstance(calendar_id, str) and continuing:
            calendar_id = continuing.get("calendar_id")
        if (
            not isinstance(calendar_id, str)
            and not isinstance(known_details.get("calendar_name"), str)
        ):
            calendar_id = "primary"

        pending_action = {
            "version": 1,
            "operation": operation,
            "original_request": original_request,
            "known_event_details": known_details,
            "unresolved_fields": unresolved_fields,
            "connection_id": str(connection_id),
            "calendar_id": calendar_id,
            "user_id": str(user_id),
            "conversation_id": str(conversation_id),
            "expires_at": (
                now + timedelta(
                    seconds=ChatService.CALENDAR_ACTION_CLARIFICATION_TTL_SECONDS
                )
            ).isoformat(),
        }
        if isinstance(draft, dict):
            pending_action["calendar_create_draft"] = draft
        return pending_action

    def get_chat_request_status(
        self,
        *,
        user_id: UUID,
        conversation_id: UUID,
        idempotency_key: UUID,
    ) -> dict[str, Any]:
        """Return idempotent request status/result scoped to the authenticated user/conversation.

        This enables safe client recovery after transport timeouts without creating
        duplicate executions. Completed results are reconstructed from persisted
        request-linked messages when available.
        """
        request = self.request_repository.get_request(
            user_id=user_id,
            conversation_id=conversation_id,
            idempotency_key=idempotency_key,
        )
        if request is None:
            raise LookupError("Chat request not found.")

        status = str(request.get("status") or "")
        payload: dict[str, Any] = {
            "conversation_id": conversation_id,
            "idempotency_key": idempotency_key,
            "status": status,
            "response": None,
            "error_message": request.get("error_message"),
        }

        if status == "completed":
            try:
                payload["response"] = self._get_completed_response(
                    user_id=user_id,
                    conversation_id=conversation_id,
                    request=request,
                )
            except Exception:
                # Request marked completed but messages not yet reconstructable; treat as processing/reconciling.
                payload["status"] = "processing"
                payload["error_message"] = None
        elif status == "processing":
            # Best-effort reconciliation if assistant message was persisted but request not finalized.
            request_id = request.get("id")
            user_message_id = request.get("user_message_id")
            if request_id and user_message_id:
                try:
                    assistant_message = self.message_service.get_request_message(
                        user_id=user_id,
                        conversation_id=conversation_id,
                        chat_request_id=UUID(str(request_id)),
                        role="assistant",
                    )
                except Exception:
                    assistant_message = None
                if assistant_message and assistant_message.get("id"):
                    lease = request.get("lease_expires_at")
                    lease_expired = False
                    if lease is not None:
                        try:
                            if lease.tzinfo is None:
                                lease = lease.replace(tzinfo=timezone.utc)
                            lease_expired = lease.astimezone(timezone.utc) <= datetime.now(timezone.utc)
                        except Exception:
                            lease_expired = False
                    if lease_expired:
                        reconciled = self.request_repository.reconcile_completed_request(
                            user_id=user_id,
                            conversation_id=conversation_id,
                            idempotency_key=idempotency_key,
                            user_message_id=UUID(str(user_message_id)),
                            assistant_message_id=UUID(str(assistant_message["id"])),
                        )
                        if reconciled is not None:
                            request = reconciled
                            payload["status"] = "completed"
                            payload["error_message"] = None
                            try:
                                payload["response"] = self._get_completed_response(
                                    user_id=user_id,
                                    conversation_id=conversation_id,
                                    request=request,
                                )
                            except Exception:
                                payload["status"] = "processing"
                                payload["response"] = None
        return payload

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
        if (
            isinstance(metadata, dict)
            and "action_approval" not in metadata
        ):
            persisted_messages = self.message_service.get_conversation_messages(
                user_id=user_id,
                conversation_id=conversation_id,
                limit=500,
            )
            source_message_id = str(assistant_message_id)
            for message in reversed(persisted_messages):
                message_metadata = message.get("metadata")
                proposal = (
                    message_metadata.get("action_approval")
                    if isinstance(message_metadata, dict)
                    else None
                )
                if (
                    isinstance(proposal, dict)
                    and proposal.get("source_message_id")
                    == source_message_id
                ):
                    metadata = {
                        **metadata,
                        "action_approval": proposal,
                    }
                    assistant_message = {
                        **assistant_message,
                        "metadata": metadata,
                    }
                    break

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

    @classmethod
    def _sanitize_stream_event(
        cls,
        payload: Any,
    ) -> dict[str, Any] | None:
        """Allowlist custom graph events; never forward raw payloads."""
        if not isinstance(payload, dict):
            return None

        event_type = payload.get("type")
        if event_type in {"token", "replace"}:
            content = payload.get("content")
            if not isinstance(content, str) or not content:
                return None
            return {
                "type": event_type,
                "content": content[: cls.STREAM_MAX_CONTENT_CHARS],
            }

        if event_type == "status":
            message = payload.get("message")
            if not isinstance(message, str):
                return None
            message = message.replace("\n", " ").strip()[:200]
            if not message:
                return None
            return {"type": "status", "message": message}

        return None

    @classmethod
    def _status_for_debug_event(cls, payload: Any) -> str | None:
        """Map a LangGraph task-start debug event to a safe status label."""
        if not isinstance(payload, dict) or payload.get("type") != "task":
            return None
        task_payload = payload.get("payload")
        if not isinstance(task_payload, dict):
            return None
        name = task_payload.get("name")
        if not isinstance(name, str):
            return None
        return cls.STREAM_NODE_STATUS.get(name)

    def _mark_chat_request_failed(
        self,
        *,
        user_id: UUID,
        conversation_id: UUID,
        idempotency_key: UUID,
        worker_id: str,
        error_message: str,
    ) -> None:
        """Persist a terminal request failure using the active worker's fence."""
        try:
            self.request_repository.update_status(
                user_id=user_id,
                conversation_id=conversation_id,
                idempotency_key=idempotency_key,
                worker_id=worker_id,
                status="failed",
                error_message=error_message,
            )
        except Exception as exc:
            log_exception_traceback(
                logger,
                "Could not persist the failed chat request status.",
                exc,
            )

    def _send_message_generator(
        self,
        user_id: UUID,
        conversation_id: UUID,
        content: str,
        idempotency_key: UUID,
        gmail_connection_id: UUID | None = None,
        user_timezone: str = "UTC",
        default_calendar_connection_id: str | None = None,
        default_contacts_connection_id: str | None = None,
        default_drive_connection_id: str | None = None,
        gmail_enabled: bool = True,
        calendar_enabled: bool = True,
        contacts_enabled: bool = True,
        drive_enabled: bool = True,
        streaming: bool = False,
    ) -> Iterator[dict[str, Any]]:
        """Process an idempotent chat request using LangGraph.

        Yields sanitized stream events (``status``, ``token``, ``replace``) and
        terminates with a single ``done`` event carrying the persisted
        response. When ``streaming`` is false the graph is invoked normally and
        no token events are produced.
        """

        if not content or not content.strip():
            raise ValueError("Message content cannot be empty.")

        normalized_content = content.strip()
        try:
            ZoneInfo(user_timezone)
        except (ValueError, KeyError) as exc:
            raise ValueError("A valid IANA timezone is required.") from exc
        request_time = datetime.now(timezone.utc)

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
            default_calendar_connection_id=default_calendar_connection_id,
            default_contacts_connection_id=default_contacts_connection_id,
            default_drive_connection_id=default_drive_connection_id,
            gmail_enabled=gmail_enabled,
            calendar_enabled=calendar_enabled,
            contacts_enabled=contacts_enabled,
            drive_enabled=drive_enabled,
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
            yield {
                "type": "done",
                "response": self._get_completed_response(
                    user_id=user_id,
                    conversation_id=conversation_id,
                    request=request,
                ),
            }
            return

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
                    yield {
                        "type": "done",
                        "response": self._get_completed_response(
                            user_id=user_id,
                            conversation_id=conversation_id,
                            request=reconciled,
                        ),
                    }
                    return

                latest_request = self.request_repository.get_request(
                    user_id=user_id,
                    conversation_id=conversation_id,
                    idempotency_key=idempotency_key,
                )

                if (
                    latest_request
                    and latest_request["status"] == "completed"
                ):
                    yield {
                        "type": "done",
                        "response": self._get_completed_response(
                            user_id=user_id,
                            conversation_id=conversation_id,
                            request=latest_request,
                        ),
                    }
                    return

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
                and current_request.get("user_message_id")
                and current_request.get("assistant_message_id")
            ):
                yield {
                    "type": "done",
                    "response": self._get_completed_response(
                        user_id=user_id,
                        conversation_id=conversation_id,
                        request=current_request,
                    ),
                }
                return

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
        # Set only once an assistant message has actually been saved. A client
        # disconnect before this point must leave a terminal request status.
        assistant_message = None

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
            history = self._hydrate_action_approval_metadata(
                history,
                user_id=user_id,
                conversation_id=conversation_id,
            )
            effective_gmail_connection_id = (
                gmail_connection_id if gmail_enabled else None
            )
            available_gmail_sources = self._available_gmail_sources(
                history,
                effective_gmail_connection_id,
            )
            granted_gmail_scopes = (
                self.user_connection_service.get_granted_google_scopes(
                    user_id=user_id,
                    connection_id=effective_gmail_connection_id,
                )
                if self.user_connection_service and gmail_connection_id
                and gmail_enabled
                else None
            )
            calendar_access = (
                self.user_connection_service.get_user_calendar_access(
                    user_id=user_id,
                    default_connection_id=default_calendar_connection_id,
                )
                if self.user_connection_service
                else {
                    "google_connection_available": False,
                    "capabilities": {},
                    "event_read_connection_id": None,
                    "freebusy_connection_id": None,
                    "default_connection_invalid": False,
                    "default_connection_selected": False,
                    "selected_connection_email": None,
                    "event_selection_required": False,
                    "freebusy_selection_required": False,
                    "selected_event_scope_missing": False,
                    "selected_freebusy_scope_missing": False,
                }
            )
            contacts_access = (
                self.user_connection_service.get_user_contacts_access(
                    user_id=user_id,
                    default_connection_id=default_contacts_connection_id,
                )
                if self.user_connection_service
                else {
                    "google_connection_available": False,
                    "capabilities": {},
                    "read_connection_id": None,
                    "write_connection_id": None,
                    "selected_connection_email": None,
                    "default_connection_invalid": False,
                    "default_connection_selected": False,
                    "selection_required": False,
                    "candidate_emails": [],
                    "read_scope_missing": False,
                    "write_scope_missing": False,
                }
            )
            contacts_connection_id = (
                contacts_access["read_connection_id"]
                if contacts_enabled
                else None
            )
            contacts_write_connection_id = (
                contacts_access["write_connection_id"]
                if contacts_enabled
                else None
            )
            drive_access = (
                self.user_connection_service.get_user_drive_access(
                    user_id=user_id,
                    default_connection_id=default_drive_connection_id,
                )
                if self.user_connection_service
                else {
                    "google_connection_available": False,
                    "capabilities": {},
                    "connection_id": None,
                    "selected_connection_email": None,
                    "default_connection_invalid": False,
                    "default_connection_selected": False,
                    "selection_required": False,
                    "candidate_emails": [],
                    "read_scope_missing": False,
                    "enabled": settings.DRIVE_CONTRACTS_ENABLED,
                }
            )
            drive_connection_id = (
                drive_access["connection_id"]
                if drive_enabled
                else None
            )
            event_read_connection_id = (
                calendar_access["event_read_connection_id"]
                if calendar_enabled
                else None
            )
            freebusy_connection_id = (
                calendar_access["freebusy_connection_id"]
                if calendar_enabled
                else None
            )
            write_connection_id = (
                calendar_access.get("write_connection_id")
                if calendar_enabled
                else None
            )
            pending_calendar_action = self._pending_calendar_action(
                history,
                user_id=user_id,
                conversation_id=conversation_id,
                connection_id=(
                    str(write_connection_id)
                    if write_connection_id
                    else None
                ),
                now=request_time,
            )
            pending_calendar_availability = self._pending_calendar_availability(
                history,
                user_id=user_id,
                conversation_id=conversation_id,
                now=request_time,
                connection_id=(
                    str(freebusy_connection_id)
                    if freebusy_connection_id
                    else None
                ),
            )
            calendar_capabilities = {
                **calendar_access["capabilities"],
                "integration_enabled": calendar_enabled,
                "events_read": bool(
                    event_read_connection_id
                ),
                "freebusy": bool(
                    freebusy_connection_id
                ),
                "api_available": bool(
                    event_read_connection_id
                    or freebusy_connection_id
                ),
                "api_complete": bool(
                    event_read_connection_id
                    and freebusy_connection_id
                ),
                "selection_required": bool(
                    calendar_enabled
                    and (
                        calendar_access["event_selection_required"]
                        or calendar_access["freebusy_selection_required"]
                    )
                ),
                "default_connection_invalid": bool(
                    calendar_access["default_connection_invalid"]
                ),
                "default_connection_selected": bool(
                    calendar_access["default_connection_selected"]
                ),
                "selected_event_scope_missing": bool(
                    calendar_access["selected_event_scope_missing"]
                ),
                "selected_freebusy_scope_missing": bool(
                    calendar_access["selected_freebusy_scope_missing"]
                ),
                "google_connection_available": (
                    calendar_access["google_connection_available"]
                ),
                "write_permission_granted": bool(
                    write_connection_id
                ),
                "write_execution_available": bool(
                    calendar_enabled
                    and
                    calendar_access.get("calendar_write_execution_available")
                ),
                "create_execution_available": bool(
                    calendar_enabled
                    and calendar_access.get(
                        "calendar_create_execution_available"
                    )
                ),
                "update_execution_available": bool(
                    calendar_enabled
                    and calendar_access.get(
                        "calendar_update_execution_available"
                    )
                ),
                "delete_execution_available": bool(
                    calendar_enabled
                    and calendar_access.get(
                        "calendar_delete_execution_available"
                    )
                ),
                "create_feature_enabled": bool(
                    calendar_enabled
                    and calendar_access.get(
                        "calendar_create_feature_enabled"
                    )
                ),
                "update_feature_enabled": bool(
                    calendar_enabled
                    and calendar_access.get(
                        "calendar_update_feature_enabled"
                    )
                ),
                "delete_feature_enabled": bool(
                    calendar_enabled
                    and calendar_access.get(
                        "calendar_delete_feature_enabled"
                    )
                ),
            }
            assistant_capabilities = build_runtime_capabilities(
                granted_scopes=granted_gmail_scopes or set(),
                unavailable_routes={
                    route
                    for route, node in ASSISTANT_ROUTE_NODES.items()
                    if node == "integration_unavailable"
                },
                calendar_capabilities=calendar_capabilities,
                drive_capabilities={
                    "connected": False,
                    "read": False,
                    "contracts_read": False,
                    "file_scope_granted": False,
                    "write_permission_granted": False,
                    "google_connection_available": False,
                    "permissions_need_consent": False,
                    "integration_enabled": False,
                    "temporarily_unavailable": True,
                    "selection_required": False,
                    "default_connection_invalid": False,
                    "default_connection_selected": False,
                    "read_scope_missing": False,
                    "chat_available": False,
                },
                contacts_capabilities={
                    **contacts_access["capabilities"],
                    "integration_enabled": contacts_enabled,
                    "connected": bool(
                        contacts_access.get("read_connection_id")
                        or contacts_access.get("write_connection_id")
                    ),
                    "google_connection_available": (
                        contacts_access["google_connection_available"]
                    ),
                    "chat_available": bool(
                        contacts_enabled
                        and contacts_access.get("read_connection_id")
                        and settings.CONTACTS_READ_ENABLED
                    ),
                    "selection_required": bool(
                        contacts_enabled
                        and contacts_access.get("selection_required")
                    ),
                    "default_connection_invalid": bool(
                        contacts_access.get("default_connection_invalid")
                    ),
                    "default_connection_selected": bool(
                        contacts_access.get("default_connection_selected")
                    ),
                    "read_scope_missing": bool(
                        contacts_access.get("read_scope_missing")
                    ),
                    "read_available": bool(
                        contacts_enabled
                        and contacts_access.get("read_connection_id")
                        and settings.CONTACTS_READ_ENABLED
                    ),
                },
            )

            # Preserve trusted approval metadata in LangGraph state. The
            # Calendar contextual-delete resolver needs the completed create
            # result (and its authoritative Google event ID) from this history.
            previous_messages = []
            for message in history:
                if (
                    message["id"] == user_message["id"]
                    or message["role"] not in {"user", "assistant"}
                ):
                    continue
                projected_message = {
                    "role": message["role"],
                    "content": message["content"],
                }
                metadata = message.get("metadata")
                if isinstance(metadata, dict) and isinstance(
                    metadata.get("action_approval"), dict
                ):
                    projected_message["metadata"] = {
                        "action_approval": metadata["action_approval"],
                    }
                previous_messages.append(projected_message)

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
                "request_time": request_time.isoformat(),
                "assistant_capabilities": assistant_capabilities,
                "memory_enabled": memory_enabled,
            }
            if pending_calendar_action is not None:
                initial_state["pending_calendar_action"] = (
                    pending_calendar_action
                )
            pending_calendar_create = self._pending_calendar_create_proposal(
                history=history,
                user_id=user_id,
                conversation_id=conversation_id,
                now=request_time,
            )
            if pending_calendar_create is not None:
                initial_state["pending_calendar_create_proposal"] = (
                    pending_calendar_create
                )
            if pending_calendar_availability is not None:
                initial_state["pending_calendar_availability"] = (
                    pending_calendar_availability
                )
            pending_calendar_delete_selection = self._pending_calendar_delete_selection(
                history,
                user_id=user_id,
                conversation_id=conversation_id,
                connection_id=(
                    str(write_connection_id) if write_connection_id else None
                ),
                now=request_time,
            )
            if pending_calendar_delete_selection is not None:
                initial_state["pending_calendar_delete_selection"] = (
                    pending_calendar_delete_selection
                )
            pending_calendar_read = self._pending_calendar_read(
                history,
                user_id=user_id,
                conversation_id=conversation_id,
                now=request_time,
            )
            if pending_calendar_read is not None:
                initial_state["pending_calendar_read"] = pending_calendar_read
            pending_gmail_clarification = self._pending_gmail_clarification(
                history,
                user_id=user_id,
                conversation_id=conversation_id,
                connection_id=effective_gmail_connection_id,
                now=request_time,
            )
            if pending_gmail_clarification is not None:
                initial_state["pending_gmail_clarification"] = pending_gmail_clarification
            if effective_gmail_connection_id is not None:
                initial_state["gmail_connection_id"] = str(
                    effective_gmail_connection_id
                )
            pending_calendar_draft = self._pending_calendar_create_draft(
                history,
            )
            # Only hydrate a standalone create draft when it is part of a valid,
            # scoped pending create clarification/action. Orphan drafts can leak
            # into unrelated requests and must not hijack routing/response.
            if (
                pending_calendar_draft is not None
                and isinstance(pending_calendar_action, dict)
                and pending_calendar_action.get("operation") == "create"
            ):
                initial_state["calendar_create_draft"] = (
                    pending_calendar_draft
                )
            pending_contacts_clarification = self._pending_contacts_clarification(
                history,
                user_id=user_id,
                conversation_id=conversation_id,
                connection_id=contacts_connection_id,
                now=request_time,
            )
            if pending_contacts_clarification is not None:
                initial_state["pending_contacts_clarification"] = (
                    pending_contacts_clarification
                )
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

            lowered_content = normalized_content.casefold()
            if "calendar" in lowered_content and "update" in lowered_content:
                log_calendar_update_diagnostic(
                    boundary="chatservice_dispatch",
                    message_length=len(normalized_content),
                    timezone_present=(
                        "Asia/Kolkata" in normalized_content
                    ),
                    module_path=__file__,
                )

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
            graph_context = {
                        "user_id": str(user_id),
                        "gmail_connection_id": (
                            str(effective_gmail_connection_id)
                            if granted_gmail_scopes is not None
                            else None
                        ),
                        "gmail_enabled": gmail_enabled,
                        "user_timezone": user_timezone,
                        "request_time": request_time.isoformat(),
                        "calendar_event_connection_id": (
                            event_read_connection_id
                        ),
                        "calendar_write_connection_id": (
                            write_connection_id
                        ),
                        "calendar_write_connection_email": (
                            calendar_access.get("write_connection_email")
                        ),
                        "calendar_freebusy_connection_id": (
                            freebusy_connection_id
                        ),
                        "calendar_enabled": calendar_enabled,
                        "google_connection_available": (
                            calendar_access["google_connection_available"]
                        ),
                        "calendar_default_connection_selected": (
                            calendar_access["default_connection_selected"]
                        ),
                        "calendar_default_connection_invalid": (
                            calendar_access["default_connection_invalid"]
                        ),
                        "calendar_selected_account_email": (
                            calendar_access["selected_connection_email"]
                        ),
                        "calendar_event_selection_required": (
                            calendar_access["event_selection_required"]
                        ),
                        "calendar_freebusy_selection_required": (
                            calendar_access["freebusy_selection_required"]
                        ),
                        "calendar_selected_event_scope_missing": (
                            calendar_access["selected_event_scope_missing"]
                        ),
                        "calendar_write_selection_required": (
                            calendar_access.get(
                                "calendar_write_selection_required",
                                False,
                            )
                        ),
                        "calendar_selected_write_scope_missing": (
                            calendar_access.get(
                                "calendar_selected_write_scope_missing",
                                False,
                            )
                        ),
                        "calendar_selected_freebusy_scope_missing": (
                            calendar_access["selected_freebusy_scope_missing"]
                        ),
                        "contacts_connection_id": (
                            contacts_connection_id
                        ),
                        "contacts_write_connection_id": (
                            contacts_write_connection_id
                        ),
                        "contacts_enabled": contacts_enabled,
                        "contacts_selection_required": (
                            contacts_enabled
                            and contacts_access["selection_required"]
                        ),
                        "contacts_selected_account_email": (
                            contacts_access["selected_connection_email"]
                        ),
                        "contacts_candidate_emails": (
                            contacts_access["candidate_emails"]
                        ),
                        "contacts_default_connection_invalid": (
                            contacts_access["default_connection_invalid"]
                        ),
                        "contacts_default_connection_selected": (
                            contacts_access["default_connection_selected"]
                        ),
                        "contacts_read_scope_missing": (
                            contacts_access["read_scope_missing"]
                        ),
                        "contacts_write_scope_missing": (
                            contacts_access["write_scope_missing"]
                        ),
                        "contacts_google_connection_available": (
                            contacts_access["google_connection_available"]
                        ),
                        "drive_connection_id": None,
                        "drive_enabled": False,
                        "drive_selection_required": False,
                        "drive_selected_account_email": (
                            None
                        ),
                        "drive_candidate_emails": (
                            []
                        ),
                        "drive_read_scope_missing": (
                            False
                        ),
                        "drive_default_connection_invalid": (
                            False
                        ),
                        "drive_default_connection_selected": (
                            False
                        ),
            }
            streamed_text = ""
            streamed_any = False
            last_status = None
            try:
                if streaming:
                    result = None
                    graph_stream = chat_graph.stream(
                        initial_state,
                        config=thread_config,
                        context=graph_context,
                        stream_mode=["custom", "debug", "values"],
                    )
                    while True:
                        # A ContextVar token may only be reset in the context
                        # that created it. Starlette resumes this generator
                        # from threadpool contexts, so streaming mode must be
                        # scoped to a single advance instead of spanning the
                        # yields below.
                        with streaming_mode():
                            try:
                                mode, payload = next(graph_stream)
                            except StopIteration:
                                break

                        if mode == "values":
                            if isinstance(payload, dict):
                                result = payload
                            continue
                        if mode == "debug":
                            status = self._status_for_debug_event(payload)
                            if status and status != last_status:
                                last_status = status
                                yield {
                                    "type": "status",
                                    "message": status,
                                }
                            continue
                        event = self._sanitize_stream_event(payload)
                        if event is None:
                            continue
                        if event["type"] == "token":
                            streamed_text += event["content"]
                            streamed_any = True
                        elif event["type"] == "replace":
                            streamed_text = event["content"]
                            streamed_any = True
                        yield event
                    if result is None:
                        raise RuntimeError(
                            "The AI workflow returned no final state."
                        )
                else:
                    result = chat_graph.invoke(
                        initial_state,
                        config=thread_config,
                        context=graph_context,
                    )
            finally:
                # Always stop heartbeat when graph execution ends,
                # whether it succeeds or raises an exception.
                self._stop_heartbeat(
                    stop_event=heartbeat_stop_event,
                    lease_lost_event=heartbeat_lease_lost_event,
                    heartbeat_thread=heartbeat_thread,
                )

            if (
                isinstance(
                    initial_state.get("pending_calendar_delete_selection"),
                    dict,
                )
                and result.get("assistant_route") != "google_calendar"
                and "pending_calendar_delete_selection" in result
            ):
                # LangGraph preserves inherited state keys. A fresh non-Calendar
                # turn must suspend (not clear) the old clarification, so omit
                # it from this turn's persisted metadata without writing None.
                result = {
                    key: value
                    for key, value in result.items()
                    if key != "pending_calendar_delete_selection"
                }

            assistant_content = result.get("assistant_response")

            if (
                not isinstance(assistant_content, str)
                or not assistant_content.strip()
            ):
                raise RuntimeError(
                    "The AI workflow returned an empty response."
                )

            # Minimal redacted lifecycle diagnostics (request -> intent/route -> graph outcome -> response).
            try:
                metrics = current_request_metrics.get()
                plan = result.get("gmail_plan") if isinstance(result.get("gmail_plan"), dict) else {}
                lifecycle_event(
                    "chat.graph.completed",
                    user_id=user_id,
                    conversation_id=conversation_id,
                    request_id=request_id,
                    assistant_route=result.get("assistant_route"),
                    intent=plan.get("intent"),
                    integration=(
                        "gmail" if result.get("assistant_route") == "gmail"
                        else "google_calendar" if result.get("assistant_route") == "google_calendar"
                        else "google_contacts" if result.get("assistant_route") == "google_contacts"
                        else "google_drive" if result.get("assistant_route") == "google_drive"
                        else "general"
                    ),
                    provider_call_occurred=bool(
                        (getattr(metrics, "google_calendar_api_call_count", 0) > 0)
                        or (getattr(metrics, "gmail_api_call_count", 0) > 0)
                        or (getattr(metrics, "google_oauth_call_count", 0) > 0)
                    ),
                    llm_call_occurred=bool(
                        (getattr(metrics, "llm_call_count", 0) > 0)
                        or (getattr(metrics, "llm_provider_call_count", 0) > 0)
                    ),
                )
            except Exception:
                logger.debug("chat.graph.completed diagnostics emission failed", exc_info=True)
            # Save memory from the user's message only.
            # Existing memory retrieval remains enabled even when
            # automatic memory storage is disabled.

            try:
                if result.get("memory_decision_executed"):
                    memory_result = None
                    logger.info(
                        "Autonomous memory decision already executed before routing | user_id=%s",
                        user_id,
                    )
                else:
                    memory_result = self.memory_pipeline.process(
                        user_id=user_id,
                        content=normalized_content,
                        memory_enabled=(
                            memory_enabled
                            and result.get("assistant_route") != "memory_update"
                        ),
                        source_conversation_id=conversation_id,
                    )
                if memory_result is not None:
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
                    if result.get("contacts_provenance"):
                        assistant_metadata["contacts_provenance"] = (
                            result["contacts_provenance"]
                        )
                    if result.get("response_metadata"):
                        assistant_metadata["response_completion"] = (
                            result["response_metadata"]
                        )
                    if isinstance(
                        result.get("calendar_approval_result"),
                        dict,
                    ):
                        assistant_metadata["action_approval"] = (
                            result["calendar_approval_result"]
                        )
                    pending_action = self._calendar_action_clarification(
                        result=result,
                        response=assistant_content,
                        current_message=normalized_content,
                        pending=pending_calendar_action,
                        user_id=user_id,
                        conversation_id=conversation_id,
                        connection_id=(
                            str(write_connection_id)
                            if write_connection_id
                            else None
                        ),
                        now=request_time,
                    )
                    if pending_action is not None:
                        assistant_metadata["pending_calendar_action"] = (
                            pending_action
                        )
                    _gmail_plan = result.get("gmail_plan")
                    _calendar_tool = (
                        _gmail_plan.get("calendar_tool")
                        if isinstance(_gmail_plan, dict)
                        else None
                    )
                    _availability_tool = _calendar_tool in {
                        "clarify",
                        "check_calendar_availability",
                    }
                    if (
                        result.get("assistant_route") == "google_calendar"
                        and _availability_tool
                        and re.search(
                            r"\b(?:free|available|availability|busy)\b",
                            normalized_content,
                            re.IGNORECASE,
                        )
                    ):
                        # Keep a short-lived, fully scoped availability context so
                        # immediate refinements ("check full day", "asia india")
                        # can resume the same local day without another broad
                        # clarification. A successful free/busy check also leaves
                        # this context because the user may still refine it.
                        _plan_args = (
                            _gmail_plan.get("calendar_arguments")
                            if isinstance(_gmail_plan, dict)
                            and isinstance(
                                _gmail_plan.get("calendar_arguments"),
                                dict,
                            )
                            else {}
                        )
                        _executed_args = result.get(
                            "calendar_executed_arguments"
                        )
                        _availability_args = (
                            _executed_args
                            if isinstance(_executed_args, dict)
                            else _plan_args
                        )
                        _availability_tz = str(
                            _availability_args.get("time_zone")
                            or user_timezone
                        )
                        _target_date: date | None = None
                        _whole_day = bool(re.search(
                            r"\b(?:full|all|whole)\s+day\b",
                            normalized_content,
                            re.IGNORECASE,
                        ))
                        try:
                            _resolved_zone = ZoneInfo(_availability_tz)
                            _raw_start = _availability_args.get("time_min")
                            _raw_end = _availability_args.get("time_max")
                            if isinstance(_raw_start, str) and isinstance(
                                _raw_end,
                                str,
                            ):
                                _start_dt = datetime.fromisoformat(
                                    _raw_start.replace("Z", "+00:00")
                                )
                                _end_dt = datetime.fromisoformat(
                                    _raw_end.replace("Z", "+00:00")
                                )
                                _target_date = _start_dt.astimezone(
                                    _resolved_zone
                                ).date()
                                _whole_day = (
                                    _start_dt.astimezone(
                                        _resolved_zone
                                    ).time()
                                    == datetime.min.time()
                                    and _end_dt.astimezone(
                                        _resolved_zone
                                    ).date()
                                    == _target_date + timedelta(days=1)
                                    and _end_dt.astimezone(
                                        _resolved_zone
                                    ).time()
                                    == datetime.min.time()
                                )
                        except (ValueError, KeyError, TypeError):
                            _target_date = None
                        if _target_date is None:
                            _temp_state = {
                                "user_timezone": _availability_tz,
                                "request_time": request_time,
                            }
                            _target_date = _relative_calendar_date(
                                normalized_content, _temp_state
                            )
                        if _target_date is None:
                            _target_date = _extract_explicit_calendar_date(
                                normalized_content,
                                reference_date=request_time.astimezone(
                                    ZoneInfo(_availability_tz)
                                ).date(),
                            )
                        assistant_metadata[
                            "pending_calendar_availability"
                        ] = {
                            "version": 1,
                            "operation": "availability",
                            "user_id": str(user_id),
                            "conversation_id": str(conversation_id),
                            "connection_id": str(
                                freebusy_connection_id
                                or event_read_connection_id
                                or write_connection_id
                                or ""
                            ),
                            "expires_at": (
                                request_time + timedelta(minutes=5)
                            ).isoformat(),
                            "original_request": normalized_content,
                            "whole_day": _whole_day,
                            "time_zone": _availability_tz,
                            "target_date": (
                                _target_date.isoformat()
                                if isinstance(_target_date, date)
                                else None
                            ),
                            "resolved": (
                                _calendar_tool
                                == "check_calendar_availability"
                            ),
                        }
                    if "calendar_create_draft" in result:
                        assistant_metadata["calendar_create_draft"] = (
                            result["calendar_create_draft"]
                        )
                    if "pending_calendar_delete_selection" in result:
                        assistant_metadata["pending_calendar_delete_selection"] = (
                            result["pending_calendar_delete_selection"]
                        )
                    if "pending_calendar_read" in result:
                        assistant_metadata["pending_calendar_read"] = (
                            result["pending_calendar_read"]
                        )
                    if "pending_contacts_clarification" in result:
                        assistant_metadata["pending_contacts_clarification"] = (
                            result["pending_contacts_clarification"]
                        )
                    result_pending_gmail = result.get("pending_gmail_clarification") if "pending_gmail_clarification" in result else None
                    if "pending_gmail_clarification" in result:
                        assistant_metadata["pending_gmail_clarification"] = result_pending_gmail
                    # If a valid pending Gmail clarification existed and this turn did not
                    # intentionally keep/refresh it, supersede/clear deterministically.
                    if pending_gmail_clarification is not None:
                        kept = assistant_metadata.get("pending_gmail_clarification")
                        if kept is None:
                            pass
                        elif isinstance(kept, dict):
                            same_pending = (
                                kept.get("type") == pending_gmail_clarification.get("type")
                                and kept.get("user_id") == pending_gmail_clarification.get("user_id")
                                and kept.get("conversation_id") == pending_gmail_clarification.get("conversation_id")
                                and kept.get("created_at") == pending_gmail_clarification.get("created_at")
                                and kept.get("expires_at") == pending_gmail_clarification.get("expires_at")
                                and kept.get("target_date") == pending_gmail_clarification.get("target_date")
                                and kept.get("period") == pending_gmail_clarification.get("period")
                            )
                            if same_pending:
                                # Keep only for explicit Gmail clarification continuation/keep flows
                                # (e.g. ambiguous fallback clarification) that intentionally preserve
                                # the same pending state. Other requests (including mixed-service
                                # clarifications) must clear stale pending.
                                clarification_reason = (
                                    (result.get("gmail_plan") or {}).get("clarification_reason")
                                    if isinstance(result.get("gmail_plan"), dict)
                                    else None
                                )
                                is_keep_continuation = (
                                    result.get("assistant_route") == "gmail"
                                    and isinstance(result.get("gmail_plan"), dict)
                                    and result.get("gmail_plan", {}).get("intent") == "clarify"
                                    and "pending_gmail_clarification" in result
                                    and result.get("pending_gmail_clarification") == pending_gmail_clarification
                                    and clarification_reason == "gmail_search_fallback_ambiguous"
                                )
                                if not is_keep_continuation:
                                    assistant_metadata["pending_gmail_clarification"] = None
                            # else: different/new pending clarification payload -> keep (e.g. emitted this turn)

                        else:
                            assistant_metadata["pending_gmail_clarification"] = None
                    elif "pending_gmail_clarification" not in assistant_metadata and "pending_gmail_clarification" not in result:
                        # No prior pending and none produced/cleared explicitly; leave absent.
                        pass
                    if isinstance(result.get("calendar_events"), list):
                        assistant_metadata["calendar_events"] = (
                            result["calendar_events"]
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

            calendar_action = result.get("calendar_action_proposal")
            if calendar_action is None:
                calendar_parameters = result.get(
                    "calendar_create_event_proposal"
                )
                if calendar_parameters is not None:
                    calendar_action = {
                        "action_type": "calendar.create_event",
                        "parameters": calendar_parameters,
                    }
            if calendar_action is not None:
                try:
                    write_connection_id = calendar_access.get(
                        "write_connection_id"
                    )
                    if not write_connection_id:
                        write_connection_id = calendar_access.get(
                            "event_connection_id"
                        )
                    if (
                        not isinstance(calendar_action, dict)
                        or not write_connection_id
                        or self.user_connection_service is None
                    ):
                        raise RuntimeError(
                            "Calendar proposal context is unavailable."
                        )
                    action_type = calendar_action.get("action_type")
                    calendar_parameters = calendar_action.get("parameters")
                    if (
                        action_type
                        not in {
                            "calendar.create_event",
                            "calendar.update_event",
                            "calendar.delete_event",
                        }
                        or not isinstance(calendar_parameters, dict)
                    ):
                        raise ValueError(
                            "Calendar proposal payload is invalid."
                        )
                    supersedes_proposal_id = result.get(
                        "calendar_proposal_supersedes_id"
                    )
                    if supersedes_proposal_id is not None and (
                        pending_calendar_create is None
                        or supersedes_proposal_id
                        != pending_calendar_create.get("proposal_id")
                        or action_type != "calendar.create_event"
                    ):
                        raise ValueError(
                            "Calendar replacement proposal context is invalid."
                        )
                    source_message_id = UUID(str(assistant_message["id"]))
                    stable_proposal_id = uuid5(
                        NAMESPACE_URL,
                        f"calendar-action-proposal:{user_id}:{request_id}:"
                        f"{action_type}",
                    )
                    proposal = ActionApprovalService(
                        self.user_connection_service.db
                    ).create_proposal(
                        user_id=user_id,
                        proposal_id=stable_proposal_id,
                        payload=ApprovalProposalCreate.model_validate({
                            "conversation_id": conversation_id,
                            "source_message_id": source_message_id,
                            "connection_id": UUID(str(write_connection_id)),
                            "action_type": action_type,
                            "parameters": calendar_parameters,
                        }),
                        supersedes_proposal_id=(
                            UUID(str(supersedes_proposal_id))
                            if supersedes_proposal_id
                            else None
                        ),
                    )
                    assistant_metadata = dict(
                        assistant_message.get("metadata") or {}
                    )
                    assistant_metadata["action_approval"] = (
                        proposal.model_dump(mode="json")
                    )
                    assistant_message = {
                        **assistant_message,
                        "metadata": assistant_metadata,
                    }
                except Exception as exc:
                    if isinstance(exc, PermissionError):
                        failure_message = (
                            "the selected Google account's Calendar write "
                            "permission could not be verified"
                        )
                    elif isinstance(exc, LookupError):
                        failure_message = (
                            "the conversation, source message, or connected "
                            "Google account could not be verified"
                        )
                    elif isinstance(exc, ValueError):
                        failure_message = (
                            "the Calendar approval details failed validation"
                        )
                    else:
                        failure_message = (
                            "a server-side storage error occurred"
                        )
                    logger.error(
                        "calendar_approval_proposal_persistence_failed "
                        "| conversation_id=%s | request_id=%s "
                        "| error_type=%s",
                        conversation_id,
                        request_id,
                        type(exc).__name__,
                    )
                    assistant_content = (
                        "I couldn’t save the Calendar approval request because "
                        f"{failure_message}. No event was created. Please "
                        "check the account and try again."
                    )
                    try:
                        updated_message = (
                            self.message_service.update_request_message_content(
                                user_id=user_id,
                                conversation_id=conversation_id,
                                chat_request_id=request_id,
                                message_id=UUID(str(assistant_message["id"])),
                                content=assistant_content,
                            )
                        )
                        if updated_message is not None:
                            assistant_message = updated_message
                        else:
                            assistant_message = {
                                **assistant_message,
                                "content": assistant_content,
                            }
                    except Exception:
                        logger.exception(
                            "Calendar proposal failure notice could not be "
                            "persisted | request_id=%s | error_type=%s",
                            request_id,
                            type(exc).__name__,
                        )
                        assistant_message = {
                            **assistant_message,
                            "content": assistant_content,
                        }

            contacts_action = result.get("contacts_action_proposal")
            if contacts_action is not None:
                try:
                    write_connection_id = contacts_write_connection_id
                    if (
                        not isinstance(contacts_action, dict)
                        or not contacts_enabled
                        or not write_connection_id
                        or self.user_connection_service is None
                    ):
                        raise PermissionError(
                            "Contacts write permission is unavailable."
                        )
                    action_type = contacts_action.get("action_type")
                    contacts_parameters = contacts_action.get("parameters")
                    if (
                        action_type
                        not in {
                            "contacts.create_contact",
                            "contacts.update_contact",
                            "contacts.delete_contact",
                        }
                        or not isinstance(contacts_parameters, dict)
                    ):
                        raise ValueError(
                            "Contacts proposal payload is invalid."
                        )
                    source_message_id = UUID(str(assistant_message["id"]))
                    stable_proposal_id = uuid5(
                        NAMESPACE_URL,
                        f"contacts-action-proposal:{user_id}:{request_id}:"
                        f"{action_type}",
                    )
                    proposal = ActionApprovalService(
                        self.user_connection_service.db
                    ).create_proposal(
                        user_id=user_id,
                        proposal_id=stable_proposal_id,
                        payload=ApprovalProposalCreate.model_validate({
                            "conversation_id": conversation_id,
                            "source_message_id": source_message_id,
                            "connection_id": UUID(str(write_connection_id)),
                            "action_type": action_type,
                            "parameters": contacts_parameters,
                        }),
                    )
                    assistant_metadata = dict(
                        assistant_message.get("metadata") or {}
                    )
                    assistant_metadata["action_approval"] = (
                        proposal.model_dump(mode="json")
                    )
                    assistant_message = {
                        **assistant_message,
                        "metadata": assistant_metadata,
                    }
                except Exception as exc:
                    if isinstance(exc, PermissionError):
                        failure_message = (
                            "Google Contacts write permission is not granted "
                            "for the selected account"
                        )
                    elif isinstance(exc, LookupError):
                        failure_message = (
                            "the conversation, source message, or selected "
                            "Google account could not be verified"
                        )
                    elif isinstance(exc, ValueError):
                        failure_message = (
                            "the Contacts approval details failed validation"
                        )
                    else:
                        failure_message = (
                            "a server-side storage error occurred"
                        )
                    logger.error(
                        "contacts_approval_proposal_persistence_failed "
                        "| conversation_id=%s | request_id=%s "
                        "| error_type=%s",
                        conversation_id,
                        request_id,
                        type(exc).__name__,
                    )
                    assistant_content = (
                        "I couldn’t save the Contacts approval request because "
                        f"{failure_message}. No contact was changed. Please "
                        "check the Connected Apps permissions and try again."
                    )
                    try:
                        updated_message = (
                            self.message_service.update_request_message_content(
                                user_id=user_id,
                                conversation_id=conversation_id,
                                chat_request_id=request_id,
                                message_id=UUID(str(assistant_message["id"])),
                                content=assistant_content,
                            )
                        )
                        if updated_message is not None:
                            assistant_message = updated_message
                        else:
                            assistant_message = {
                                **assistant_message,
                                "content": assistant_content,
                            }
                    except Exception:
                        assistant_message = {
                            **assistant_message,
                            "content": assistant_content,
                        }

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
            try:
                lifecycle_event(
                    "chat.response.completed",
                    user_id=user_id,
                    conversation_id=conversation_id,
                    request_id=request_id,
                    assistant_route=result.get("assistant_route") if isinstance(result, dict) else None,
                    intent=((result.get("gmail_plan") or {}).get("intent") if isinstance(result, dict) and isinstance(result.get("gmail_plan"), dict) else None),
                    proposal_id=(metadata.get("action_approval") or {}).get("id") if isinstance(metadata.get("action_approval"), dict) else None,
                    status_transition="processing->completed",
                )
            except Exception:
                logger.debug("chat.response.completed diagnostics emission failed", exc_info=True)

            response = {
                "conversation_id": conversation_id,
                "user_message": user_message,
                "assistant_message": assistant_message,
                "provider": metadata.get("provider", ""),
                "model": metadata.get("model", ""),
            }

            if streaming:
                # Guarantee that what the client rendered equals what was
                # persisted, including deterministic non-LLM routes and
                # post-generation rewrites that did not emit their own replace.
                final_text = assistant_message.get("content") or assistant_content
                if not streamed_any:
                    yield {"type": "token", "content": final_text}
                elif final_text != streamed_text:
                    yield {"type": "replace", "content": final_text}

            yield {"type": "done", "response": response}
            return

        except ChatRequestUncertainError:
            raise

        except GeneratorExit:
            # A mid-stream client disconnect is not a completed response. When
            # nothing was persisted, mark the request failed so a retry gets
            # deterministic terminal guidance instead of a stuck conflict.
            # If the assistant message was already saved, leave it untouched
            # so the reconcile path can still complete the original request.
            if assistant_message is None:
                self._mark_chat_request_failed(
                    user_id=user_id,
                    conversation_id=conversation_id,
                    idempotency_key=idempotency_key,
                    worker_id=worker_id,
                    error_message=(
                        "Chat request ended before the assistant response was saved."
                    ),
                )
            raise

        except Exception:
            # Only the active worker can mark the request failed.
            self._mark_chat_request_failed(
                user_id=user_id,
                conversation_id=conversation_id,
                idempotency_key=idempotency_key,
                worker_id=worker_id,
                error_message="Chat execution failed.",
            )
            raise

    def send_message(
        self,
        user_id: UUID,
        conversation_id: UUID,
        content: str,
        idempotency_key: UUID,
        gmail_connection_id: UUID | None = None,
        user_timezone: str = "UTC",
        default_calendar_connection_id: str | None = None,
        default_contacts_connection_id: str | None = None,
        default_drive_connection_id: str | None = None,
        gmail_enabled: bool = True,
        calendar_enabled: bool = True,
        contacts_enabled: bool = True,
        drive_enabled: bool = True,
    ) -> dict[str, Any]:
        """Process an idempotent chat request and return the final response."""
        for event in self._send_message_generator(
            user_id=user_id,
            conversation_id=conversation_id,
            content=content,
            idempotency_key=idempotency_key,
            gmail_connection_id=gmail_connection_id,
            user_timezone=user_timezone,
            default_calendar_connection_id=default_calendar_connection_id,
            default_contacts_connection_id=default_contacts_connection_id,
            default_drive_connection_id=default_drive_connection_id,
            gmail_enabled=gmail_enabled,
            calendar_enabled=calendar_enabled,
            contacts_enabled=contacts_enabled,
            drive_enabled=drive_enabled,
            streaming=False,
        ):
            if event.get("type") == "done":
                return event["response"]

        raise RuntimeError("Chat execution ended without a result.")

    def stream_message(
        self,
        user_id: UUID,
        conversation_id: UUID,
        content: str,
        idempotency_key: UUID,
        gmail_connection_id: UUID | None = None,
        user_timezone: str = "UTC",
        default_calendar_connection_id: str | None = None,
        default_contacts_connection_id: str | None = None,
        default_drive_connection_id: str | None = None,
        gmail_enabled: bool = True,
        calendar_enabled: bool = True,
        contacts_enabled: bool = True,
        drive_enabled: bool = True,
    ) -> Iterator[dict[str, Any]]:
        """Stream a chat request, yielding tokens as the graph produces them.

        Pre-flight failures are raised before the first event so the HTTP layer
        can still return a proper error status. Failures after streaming has
        started propagate from the iterator; the request is marked failed and
        the partial assistant message is never persisted as completed.
        """
        yield from self._send_message_generator(
            user_id=user_id,
            conversation_id=conversation_id,
            content=content,
            idempotency_key=idempotency_key,
            gmail_connection_id=gmail_connection_id,
            user_timezone=user_timezone,
            default_calendar_connection_id=default_calendar_connection_id,
            default_contacts_connection_id=default_contacts_connection_id,
            default_drive_connection_id=default_drive_connection_id,
            gmail_enabled=gmail_enabled,
            calendar_enabled=calendar_enabled,
            contacts_enabled=contacts_enabled,
            drive_enabled=drive_enabled,
            streaming=True,
        )
