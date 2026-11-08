import logging
from typing import Any
from uuid import UUID

from app.db.session import SessionLocal
from app.memory.memory_decision import MemoryDecisionEngine
from app.memory.memory_pipeline import MemoryPipeline
from app.services.memory_service import MemoryService
from app.workflows.chat_state import ChatState
from app.workflows.memory_update_node import (
    _compact_time_range,
    _TIME_RANGE_PATTERN,
)
from app.workflows.prompt_normalization import normalize_prompt_for_routing


logger = logging.getLogger(__name__)


def _is_explicit_calendar_request(text: str) -> bool:
    lowered = text.casefold()
    return bool(
        "google calendar" in lowered
        or (
            "calendar" in lowered
            and any(
                term in lowered
                for term in (
                    "event",
                    "meeting",
                    "appointment",
                    "update",
                    "change",
                    "move",
                    "add",
                    "create",
                )
            )
        )
    )


def _is_update_change(text: str) -> bool:
    lowered = text.casefold()
    return bool(
        _TIME_RANGE_PATTERN.search(text)
        and any(
            cue in lowered
            for cue in (
                "moved",
                "has moved",
                "changed",
                "has changed",
                "new time",
                "instead of",
                "update my stored",
                "update this information in my memory",
            )
        )
    )


def _find_ambiguous_update_matches(
    user_id: UUID,
    old_time: str,
) -> list[Any]:
    old_compact = _compact_time_range(old_time)
    with SessionLocal() as session:
        memories = MemoryService(session).list_memories(
            user_id=user_id,
            memory_type="episodic",
            limit=100,
            offset=0,
        )
        matches = []
        for memory in memories:
            if any(
                _compact_time_range(stored.group(0)) == old_compact
                for stored in _TIME_RANGE_PATTERN.finditer(memory.content)
            ):
                matches.append(memory)
        return matches


def autonomous_memory_node(state: ChatState) -> dict[str, Any]:
    """Make an autonomous memory decision before external-tool routing."""
    trace = {
        "first_node": "autonomous_memory",
        "memory_decision": "IGNORE",
        "selected_memory_id": None,
        "next_node": "intent_router",
        "gmail_node_executed": False,
        "calendar_node_executed": False,
    }
    text = state["user_message"].strip()
    if not text:
        return {
            "memory_decision_result": {
                "action": "IGNORE",
                "memory_type": None,
                "reason": "No conversation content was provided.",
            },
            "memory_decision_executed": True,
            "memory_execution_trace": trace,
        }

    user_id = UUID(state["user_id"])
    routing_text = normalize_prompt_for_routing(text)
    calendar_request = _is_explicit_calendar_request(routing_text)
    ranges = list(_TIME_RANGE_PATTERN.finditer(routing_text))

    if (
        not calendar_request
        and _is_update_change(routing_text)
        and len(ranges) >= 2
    ):
        old_time = ranges[0].group(0)
        matches = _find_ambiguous_update_matches(user_id, old_time)
        if len(matches) > 1:
            action = "REVIEW"
            confidence = 0.95
            reason = "Multiple matching episodic memories are ambiguous."
        elif len(matches) == 1:
            action = "UPDATE"
            confidence = 0.98
            reason = "A unique episodic memory matches the old event time."
        else:
            action = "IGNORE"
            confidence = 0.80
            reason = "No existing episodic memory safely matches the change."

        decision = MemoryDecisionEngine.create(
            action=action,
            memory_type="episodic",
            target_memory_id=matches[0].id if len(matches) == 1 else None,
            proposed_content=text,
            confidence=confidence,
            importance=7,
            reason=reason,
        )

        if action in {"UPDATE", "REVIEW"}:
            trace.update({
                "memory_decision": action,
                "selected_memory_id": (
                    str(matches[0].id) if len(matches) == 1 else None
                ),
                "next_node": "memory_update",
            })
            return {
                "memory_decision_result": decision.model_dump(mode="json"),
                "memory_decision_executed": True,
                "memory_execution_trace": trace,
                "assistant_route": "memory_update",
                "gmail_plan": {
                    "intent": "memory_update",
                    "memory_scope": "episodic",
                },
            }

    if state.get("memory_enabled", True):
        try:
            with SessionLocal() as session:
                result = MemoryPipeline(session).process(
                    user_id=user_id,
                    content=text,
                    memory_enabled=True,
                    source_conversation_id=(
                        UUID(state["conversation_id"])
                        if state.get("conversation_id")
                        else None
                    ),
                )
            logger.info(
                "Autonomous memory decision completed | status=%s",
                result.status,
            )
            decision_items = [
                item.decision.model_dump(mode="json")
                for item in result.items
                if item.decision is not None
            ]
            return {
                "memory_decision_result": {
                    "action": (
                        "CREATE"
                        if result.status == "stored"
                        else result.status.upper()
                        if result.status in {"updated", "duplicate", "review_required"}
                        else "IGNORE"
                    ),
                    "status": result.status,
                    "items": decision_items,
                    "reason": result.reason,
                },
                "memory_decision_executed": True,
                "memory_execution_trace": trace,
            }
        except Exception:
            logger.exception(
                "Autonomous memory decision failed; continuing safely."
            )

    return {
        "memory_decision_result": {
            "action": "IGNORE",
            "memory_type": None,
            "reason": "No memory change was appropriate.",
        },
        "memory_decision_executed": True,
        "memory_execution_trace": trace,
    }
