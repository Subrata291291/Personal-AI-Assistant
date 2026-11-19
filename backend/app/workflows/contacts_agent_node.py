import logging
import re
from typing import Any

from langgraph.runtime import Runtime

from app.workflows.chat_state import ChatContext, ChatState
from app.workflows.contacts_read import generate_contacts_read_response
from app.llm.streaming import emit_status


logger = logging.getLogger(__name__)


def _extract_contact_params(message: str) -> dict[str, Any]:
    params: dict[str, Any] = {}
    name_match = re.search(
        r"\b(?:named|called|titled)\s+([A-Za-z][A-Za-z0-9 '&-]{1,40})",
        message,
        re.IGNORECASE,
    )
    if name_match:
        params["display_name"] = name_match.group(1).strip()
    else:
        for_match = re.search(
            r"\bfor\s+(?:a\s+|an\s+|the\s+)?([A-Za-z][A-Za-z0-9 '&-]{1,40})\s*$",
            message,
            re.IGNORECASE,
        )
        if for_match:
            params["display_name"] = for_match.group(1).strip()

    email_match = re.search(
        r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
        message,
    )
    if email_match:
        params["email"] = email_match.group(0)

    phone_match = re.search(r"\b(?:\+?\d[\d\s\-()]{7,}\d)\b", message)
    if phone_match:
        params["phone"] = phone_match.group(0).strip()

    return params


def contacts_agent_node(
    state: ChatState,
    runtime: Runtime[ChatContext],
) -> dict[str, Any]:
    """Handle Google Contacts read requests. Contacts mutations are unavailable."""

    emit_status("Retrieving contact details...")
    context = runtime.context
    user_id = context.get("user_id")
    message = state["user_message"]
    lowered = message.casefold()
    connection_id = context.get("contacts_connection_id")

    has_pending_clarification = isinstance(
        state.get("pending_contacts_clarification"),
        dict,
    )
    google_connection_available = bool(
        context.get("contacts_google_connection_available")
        if context.get("contacts_google_connection_available") is not None
        else context.get("google_connection_available", False)
    )
    mutation_pattern = re.compile(
        r"\b(?:create|add|new|save|update|edit|change|modify|rename|"
        r"delete|remove)\b.{0,80}\b(?:contact|email|phone|number|"
        r"address|organization|job title)\b|"
        r"\b(?:change|update|edit|delete|remove)\b.{0,40}"
        r"\b(?:his|her|their|its)\b.{0,40}"
        r"\b(?:number|email|address|contact)\b",
        re.IGNORECASE,
    )
    read_pattern = re.compile(
        r"\b(?:list|show|find|search|get|look up|lookup|who is|who's|"
        r"what is|what's|what are|tell me|give me|do i have|how many|"
        r"count|contact|contacts|phone|number|mobile|email|address|"
        r"organization|job title|details?)\b",
        re.IGNORECASE,
    )
    is_mutation_request = bool(mutation_pattern.search(lowered))
    is_read_request = (
        bool(read_pattern.search(lowered))
        or has_pending_clarification
        or not is_mutation_request
    )

    if context.get("contacts_enabled", True) is False:
        logger.info(
            "contacts_agent_stage | stage=blocked | reason=integration_disabled"
        )
        response = (
            "Google Contacts is turned off in Connected Apps. Turn it on "
            "to search and view your contacts."
        )
        return {
            "assistant_response": response,
            "provider": "",
            "model": "",
            "messages": [
                {"role": "user", "content": state["user_message"]},
                {"role": "assistant", "content": response},
            ],
            "pending_contacts_clarification": None,
        }

    if context.get("contacts_selection_required", False):
        logger.info(
            "contacts_agent_stage | stage=blocked | reason=selection_required"
        )
        candidate_emails = [
            email for email in (context.get("contacts_candidate_emails") or [])
            if email
        ]
        account_text = ", ".join(candidate_emails)
        response = (
            "More than one connected Google account has Contacts access. "
            "Choose which account Contacts should use in Connected Apps"
            + (f" ({account_text})." if account_text else ".")
        )
        return {
            "assistant_response": response,
            "provider": "",
            "model": "",
            "messages": [
                {"role": "user", "content": state["user_message"]},
                {"role": "assistant", "content": response},
            ],
            "pending_contacts_clarification": None,
        }

    if context.get("contacts_default_connection_invalid", False):
        logger.info(
            "contacts_agent_stage | stage=blocked | reason=default_connection_invalid"
        )
        response = (
            "The selected Contacts account is no longer available. "
            "Choose a different Google account in Connected Apps."
        )
        return {
            "assistant_response": response,
            "provider": "",
            "model": "",
            "messages": [
                {"role": "user", "content": state["user_message"]},
                {"role": "assistant", "content": response},
            ],
            "pending_contacts_clarification": None,
        }

    if not connection_id:
        if not google_connection_available:
            reason = "no_google_connection"
            response = (
                "Google Contacts is not connected. Connect a Google account "
                "in Connected Apps and grant Contacts read access."
            )
        elif context.get("contacts_read_scope_missing", False):
            reason = "read_scope_missing"
            response = (
                "Google Contacts read permission has not been granted for "
                "the selected account. Grant Contacts read access in "
                "Connected Apps."
            )
        else:
            reason = "read_connection_unavailable"
            response = (
                "I can’t access Google Contacts right now. Check your "
                "connected Google account and Contacts permissions in "
                "Connected Apps."
            )
        logger.info(
            "contacts_agent_stage | stage=blocked | reason=%s | google_connection_available=%s",
            reason,
            google_connection_available,
        )
        return {
            "assistant_response": response,
            "provider": "",
            "model": "",
            "messages": [
                {"role": "user", "content": state["user_message"]},
                {"role": "assistant", "content": response},
            ],
            "pending_contacts_clarification": None,
        }

    if is_mutation_request:
        logger.info(
            "contacts_agent_stage | stage=blocked | reason=mutation_not_available"
        )
        response = (
            "I can help you find and view Google Contacts, but creating, "
            "updating, and deleting contacts is not available right now. "
            "I haven’t made any changes. If you’d like, tell me whose "
            "contact details you want to look up."
        )
        return {
            "assistant_response": response,
            "provider": "",
            "model": "",
            "messages": [
                {"role": "user", "content": state["user_message"]},
                {"role": "assistant", "content": response},
            ],
            "pending_contacts_clarification": None,
        }

    # Read-only workflow (grounded). No Google IDs exposed by the LLM.
    logger.info(
        "contacts_agent_stage | stage=read_start | has_pending_clarification=%s",
        has_pending_clarification,
    )
    try:
        result = generate_contacts_read_response(
            user_id=str(user_id or context.get("user_id")),
            connection_id=str(connection_id),
            message=message,
            conversation_id=(
                str(state.get("conversation_id"))
                if state.get("conversation_id")
                else None
            ),
            pending_clarification=(
                state.get("pending_contacts_clarification")
                if has_pending_clarification
                else None
            ),
        )
        if "pending_contacts_clarification" not in result:
            result["pending_contacts_clarification"] = None
        logger.info(
            "contacts_agent_stage | stage=read_complete | status=%s",
            "ok" if result.get("assistant_response") else "empty",
        )
        return result
    except Exception:
        logger.exception(
            "contacts_agent_stage | stage=read_failed | reason=unexpected"
        )
        response = (
            "I couldn't retrieve your Google Contacts right now. "
            "Please try again."
        )
        return {
            "assistant_response": response,
            "provider": "",
            "model": "",
            "messages": [
                {"role": "user", "content": state["user_message"]},
                {"role": "assistant", "content": response},
            ],
            "pending_contacts_clarification": None,
        }