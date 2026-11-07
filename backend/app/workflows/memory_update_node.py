import logging
import re
from typing import Any
from uuid import UUID

from app.db.session import SessionLocal
from app.services.memory_service import MemoryService
from app.workflows.chat_state import ChatState


logger = logging.getLogger(__name__)

_TIME_RANGE_PATTERN = re.compile(
    r"\b(?:"
    r"\d{1,2}:\d{2}\s*(?:(?:a|p)\.?m\.?|(?:-|–|—|\bto\b))?"
    r"(?:\s*(?:a|p)\.?m\.?)?\s*(?:-|–|—|\bto\b)\s*"
    r"\d{1,2}:\d{2}(?:\s*(?:a|p)\.?m\.?)?"
    r"|"
    r"\d{1,2}(?::\d{2})?\s*(?:a|p)\.?m\.?\s*(?:-|–|—|\bto\b)\s*"
    r"\d{1,2}(?::\d{2})?\s*(?:a|p)\.?m\.?"
    r"|"
    r"\d{1,2}\s*(?:a|p)\.?m\.?\s*(?:-|–|—|\bto\b)\s*"
    r"\d{1,2}\s*(?:a|p)\.?m\.?"
    r")\b",
    re.IGNORECASE,
)


def _compact_time_range(value: str) -> str:
    parts = re.split(r"\s*(?:-|–|—|\bto\b)\s*", value.strip(), maxsplit=1, flags=re.IGNORECASE)
    if len(parts) != 2:
        return re.sub(r"\s+", "", value).casefold()
    return "-".join(_normalize_time_token(token) for token in parts)


def _normalize_time_token(value: str) -> str:
    token = re.sub(r"\s+", "", value.strip().casefold())
    match = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?(a\.?m\.?|p\.?m\.?)?", token, re.IGNORECASE)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2) or "0")
        meridiem = (match.group(3) or "").replace(".", "")
        if meridiem == "pm" and hour < 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
        return f"{hour:02d}:{minute:02d}"
    return token


def _replace_time_range(content: str, old_time: str, new_time: str) -> str:
    old_range_compact = _compact_time_range(old_time)
    matches = list(_TIME_RANGE_PATTERN.finditer(content))
    for match in reversed(matches):
        if _compact_time_range(match.group(0)) == old_range_compact:
            return content[:match.start()] + new_time + content[match.end():]
    return content


def memory_update_node(state: ChatState) -> dict[str, Any]:
    """Safely update one uniquely matching episodic memory."""
    query = state["user_message"].strip()
    if not query:
        return {
            "memory_update_result": {"status": "rejected"},
            "assistant_response": "No memory update details were provided.",
        }

    ranges = _TIME_RANGE_PATTERN.findall(query)
    if len(ranges) < 2:
        return {
            "memory_update_result": {
                "status": "clarification_required",
                "reason": "Both the existing and new meeting times are required.",
            },
            "assistant_response": (
                "Please provide both the previous meeting time and the new meeting time "
                "so I can update the stored memory safely."
            ),
        }

    if re.search(r"\binstead\s+of\b", query.casefold()):
        instead_index = query.casefold().find("instead of")
        old_matches = list(_TIME_RANGE_PATTERN.finditer(query[instead_index:]))
        new_matches = list(_TIME_RANGE_PATTERN.finditer(query[:instead_index]))
        if old_matches and new_matches:
            old_time = old_matches[0].group(0)
            new_time = new_matches[-1].group(0)
        else:
            old_time, new_time = ranges[-1], ranges[0]
    else:
        old_time, new_time = ranges[0], ranges[-1]
    user_id = UUID(state["user_id"])

    try:
        with SessionLocal() as session:
            memory_service = MemoryService(session)
            memories = memory_service.list_memories(
                user_id=user_id,
                memory_type="episodic",
                limit=100,
                offset=0,
            )

            candidates = []
            old_compact = _compact_time_range(old_time)
            for memory in memories:
                content = memory.content
                stored_ranges = _TIME_RANGE_PATTERN.findall(content)
                exact_old = any(
                    _compact_time_range(stored_range) == old_compact
                    for stored_range in stored_ranges
                )
                if exact_old or (
                    re.search(r"\bmeeting\b", query.casefold())
                    and re.search(r"\bmeeting\b", content.casefold())
                ):
                    candidates.append(memory)

            if not candidates:
                return {
                    "memory_update_result": {
                        "status": "clarification_required",
                        "reason": "I couldn't identify the matching stored meeting memory.",
                    },
                    "assistant_response": (
                        "I couldn't identify the matching stored meeting memory. "
                        "Please clarify which event or time you want me to update."
                    ),
                }

            exact = [
                memory for memory in candidates
                if any(
                    _compact_time_range(stored_range) == old_compact
                    for stored_range in _TIME_RANGE_PATTERN.findall(memory.content)
                )
            ]
            matches = exact or candidates
            if len(matches) > 1:
                return {
                    "memory_update_result": {
                        "status": "clarification_required",
                        "reason": "Multiple episodic memories could match; please specify which record to update.",
                    },
                    "assistant_response": (
                        "Multiple stored meeting memories could match. "
                        "Please specify which record I should update."
                    ),
                }

            memory = matches[0]
            updated_content = _replace_time_range(
                memory.content,
                old_time,
                new_time,
            )
            if updated_content == memory.content:
                updated_content = memory.content.replace(old_time, new_time)

            if updated_content == memory.content:
                return {
                    "memory_update_result": {
                        "status": "clarification_required",
                        "reason": "The stored memory did not contain an exact time that could be safely replaced.",
                    },
                    "assistant_response": (
                        "The stored memory did not contain an exact time I could safely replace. "
                        "Please clarify which record should change."
                    ),
                }

            updated = memory_service.update_memory(
                user_id=user_id,
                memory_id=memory.id,
                content=updated_content,
            )
            if updated is None:
                return {
                    "memory_update_result": {
                        "status": "failed",
                        "reason": "The existing memory could not be updated.",
                    },
                    "assistant_response": (
                        "The existing memory could not be updated. No change was persisted."
                    ),
                }

            logger.info(
                "Episodic memory update persisted | memory_id=%s",
                updated.id,
            )
            return {
                "memory_update_result": {
                    "status": "updated",
                    "memory_id": str(updated.id),
                    "content": updated.content,
                },
                "memory_execution_trace": {
                    "first_node": "autonomous_memory",
                    "memory_decision": "UPDATE",
                    "selected_memory_id": str(updated.id),
                    "next_node": "memory_update",
                    "gmail_node_executed": False,
                    "calendar_node_executed": False,
                },
                "assistant_response": (
                    f"I updated the stored meeting memory. "
                    f"It is now scheduled from {new_time}."
                ),
            }
    except Exception:
        logger.exception("Episodic memory update failed.")
        return {
            "memory_update_result": {
                "status": "failed",
                "reason": "The memory update could not be persisted.",
            },
            "assistant_response": (
                "The memory update could not be persisted. No change was confirmed."
            ),
        }
