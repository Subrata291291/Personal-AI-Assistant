import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from uuid import uuid4
import hashlib
import logging

try:
    from app.core.request_metrics import request_id as _request_id
except Exception:
    def _request_id():
        return None

from app.llm.router import llm_router
from app.llm.schemas import LLMMessage, LLMRequest
from app.schemas.contacts_read import ContactsReadExtraction
from app.workflows.contacts_tools import create_contacts_tools



logger = logging.getLogger(__name__)


def _safe_ref(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]
    except Exception:
        return None

ContactsReadIntentType = Literal[
    "list_contacts",
    "count_contacts",
    "search_contact",
    "get_contact_phone",
    "get_contact_email",
    "get_contact_details",
    "unknown",
]
RequestedAttribute = Literal["phone", "email", "details", "none"]

CONTACTS_CLARIFICATION_TTL_SECONDS = 30 * 60
CONTACTS_SEARCH_PAGE_SIZE = 10
CONTACTS_SEARCH_MAX_PAGES = 2
CONTACTS_SEARCH_MAX_RESULTS = 20
# Bounded search limits (read-only safety): stop after max pages/results and report
# incomplete search when no confident resolution is possible within the scanned set.

_RESOURCE_NAME_RE = re.compile(r"^people/[A-Za-z0-9._-]{1,505}\Z")


@dataclass(frozen=True)
class ContactsReadIntent:
    intent: ContactsReadIntentType
    requested_attribute: RequestedAttribute
    target_query: str | None
    ambiguous: bool = False
    reason: str | None = None
    source: str = "deterministic"  # deterministic | llm | clarification


_PHONE_WORDS = re.compile(
    r"\b(?:contact\s*(?:no|number|num)|phone\s*(?:no|number|num)?|mobile\s*(?:no|number|num)?|telephone|tel|number)\b",
    re.IGNORECASE,
)
_EMAIL_WORDS = re.compile(
    r"\b(?:email|e-mail|mail\s+id|mailid|email\s+address)\b",
    re.IGNORECASE,
)
_DETAILS_WORDS = re.compile(
    r"\b(?:contact\s+details|details|info|information|profile|everything|all\s+details|address|location)\b",
    re.IGNORECASE,
)
_COUNT_PATTERNS = [
    re.compile(r"\bhow\s+many\s+contacts\b", re.IGNORECASE),
    re.compile(r"\bcount\s+(?:my\s+|all\s+)?contacts\b", re.IGNORECASE),
    re.compile(r"\bnumber\s+of\s+contacts\b", re.IGNORECASE),
    re.compile(r"\btotal\s+(?:number\s+of\s+)?contacts\b", re.IGNORECASE),
]
_LIST_ALL_PATTERNS = [
    re.compile(r"\b(?:show|list|display|give|see|view)\b.*\b(?:all\s+)?contacts\b", re.IGNORECASE),
    re.compile(r"\b(?:all\s+contacts|my\s+contacts|contacts\s+list)\b", re.IGNORECASE),
]
_SPECIFIC_PREFIXES = re.compile(
    r"^(?:please\s+|can\s+you\s+|could\s+you\s+|would\s+you\s+|will\s+you\s+|hey\s+|hi\s+|ok\s+|okay\s+)*"
    r"(?:check\s+(?:for\s+)?|show\s+me\s+|show\s+|tell\s+me\s+|give\s+me\s+|find\s+(?:me\s+)?|search\s+(?:for\s+)?|get\s+(?:me\s+)?|fetch\s+(?:me\s+)?|look\s*up\s+|lookup\s+|what\s+is\s+|what'?s\s+|whats\s+|do\s+i\s+have\s+|is\s+there\s+)?",
    re.IGNORECASE,
)
_TRAILING_REQUEST = re.compile(
    r"(?:\s*[,;:\-–—]\s*|\s+)(?:'s\s+|’s\s+|s\s+)?(?:contact\s*(?:no|number|num)|phone\s*(?:no|number|num)?|mobile\s*(?:no|number|num)?|telephone|tel|number|email\s*(?:address|id)?|e-mail\s*(?:address|id)?|mail\s*id|contact\s+details|details|info|information|profile|address|location)\s*$",
    re.IGNORECASE,
)
_LEADING_FOR = re.compile(
    r"^(?:the\s+)?(?:contact\s*(?:no|number|num|details|info)?|phone\s*(?:no|number|num)?|mobile\s*(?:no|number|num)?|email\s*(?:address|id)?|details|info|information|address)\s+(?:of|for)\s+",
    re.IGNORECASE,
)
_NAMED_FOR = re.compile(r"\b(?:named|called|contact\s+named)\s+(?P<name>.+)$", re.IGNORECASE)
_FOR_SUFFIX = re.compile(r"\bfor\s+(?P<name>.+)$", re.IGNORECASE)
_PRONOUN_TARGET_RE = re.compile(
    r"^(?:show\s+me\s+|tell\s+me\s+|give\s+me\s+|what(?:'s| is)?\s+)?(?:his|her|their|its|that|the|this)\b",
    re.IGNORECASE,
)
_SELECTION_ONLY_RE = re.compile(
    r"^\s*(?:(?:the\s+)?(?:first|second|third|fourth|fifth|1st|2nd|3rd|4th|5th)(?:\s+(?:one|contact|person))?|(?:option\s*)?[1-9][0-9]?|[1-9][0-9]?[\).:]?)\s*$",
    re.IGNORECASE,
)


def _clean_target(text: str) -> str | None:
    candidate = text.strip().strip("?.!,;:\"' ")
    # Conversational correction: "Not Boro Mami, I want Mam number" -> "Mam number"
    m_corr = re.search(
        r"\b(?:not|no,?\s*not)\b.+?(?:[,.;!?]\s*|\s+)(?:i\s+(?:want|mean|meant)|show\s+me|give\s+me|looking\s+for|need)\b\s*(?P<want>.+)$",
        candidate,
        flags=re.IGNORECASE | re.UNICODE,
    )
    if m_corr and m_corr.group("want"):
        candidate = m_corr.group("want").strip().strip("?.!,;:\"' ")
    else:
        # Also handle "I mean/want Mam" phrased as correction without explicit "not X," prefix capture.
        m_want = re.search(
            r"\b(?:i\s+(?:want|mean|meant)|show\s+me|give\s+me)\b\s+(?:the\s+)?(?:contact\s+(?:named|called)\s+)?(?P<want>.+)$",
            candidate,
            flags=re.IGNORECASE | re.UNICODE,
        )
        if m_want and m_want.group("want") and re.search(r"\b(?:not|no\b|other)\b", candidate, flags=re.IGNORECASE):
            candidate = m_want.group("want").strip().strip("?.!,;:\"' ")
    for _ in range(4):
        new = _SPECIFIC_PREFIXES.sub("", candidate).strip().strip("?.!,;:\"' ")
        new = _LEADING_FOR.sub("", new).strip().strip("?.!,;:\"' ")
        new = re.sub(r"^(?:check|find|search|show|get|tell|give|look\s*up|lookup|for|me|my)\b[\s:,-]*", "", new, flags=re.IGNORECASE).strip().strip("?.!,;:\"' ")
        if new == candidate:
            break
        candidate = new
    m = _NAMED_FOR.search(candidate)
    if m:
        candidate = m.group("name")
    else:
        m = _FOR_SUFFIX.search(candidate)
        if m and len(m.group("name").strip()) <= 80:
            candidate = m.group("name")
    candidate = _TRAILING_REQUEST.sub("", candidate).strip().strip("?.!,;:\"' ")
    candidate = re.sub(r"(?:'s|’s)\s*$", "", candidate, flags=re.IGNORECASE).strip().strip("?.!,;:\"' ")
    candidate = re.sub(r"\b(?:please|now|for me|to me)\b$", "", candidate, flags=re.IGNORECASE).strip().strip("?.!,;:\"' ")
    candidate = re.sub(r"\s+", " ", candidate).strip()
    if not candidate:
        return None
    lowered_candidate = candidate.casefold()
    if lowered_candidate in {
        "contact", "contacts", "number", "phone", "email", "details", "all",
        "the", "a", "an", "my", "me", "his", "her", "their", "its", "someone", "somebody", "person",
        "first", "second", "third", "one", "that", "this",
    }:
        return None
    if len(candidate) < 2:
        return None
    if len(candidate) > 120:
        candidate = candidate[:120].strip()
    return candidate or None


def _detect_requested_attribute(text: str) -> RequestedAttribute:
    if _PHONE_WORDS.search(text):
        return "phone"
    if _EMAIL_WORDS.search(text):
        return "email"
    if _DETAILS_WORDS.search(text):
        return "details"
    return "none"


def extract_contacts_read_intent(message: str) -> ContactsReadIntent:
    """Rule-based Contacts read intent extraction (deterministic baseline/fallback)."""
    text = (message or "").strip()
    lowered = text.casefold()

    if not text:
        return ContactsReadIntent("unknown", "none", None, ambiguous=True, reason="empty_message", source="deterministic")

    for pattern in _COUNT_PATTERNS:
        if pattern.search(text):
            return ContactsReadIntent("count_contacts", "none", None, source="deterministic")

    requested_attribute = _detect_requested_attribute(text)
    has_named = bool(re.search(r"\b(?:named|called)\b", lowered))
    has_for_of = bool(re.search(r"\b(?:of|for)\s+[^\s]", lowered))
    has_possessive = bool(re.search(r"\b[\w][\w .'-]{0,60}'s\b", text))
    specific_signal = has_named or has_for_of or has_possessive or (
        requested_attribute != "none" and re.search(r"\b(?:of|for|named|called)\b", lowered)
    )

    if requested_attribute != "none" or specific_signal:
        target = _clean_target(text)
        if target:
            if requested_attribute == "phone":
                return ContactsReadIntent("get_contact_phone", "phone", target, source="deterministic")
            if requested_attribute == "email":
                return ContactsReadIntent("get_contact_email", "email", target, source="deterministic")
            if requested_attribute == "details":
                return ContactsReadIntent("get_contact_details", "details", target, source="deterministic")
            return ContactsReadIntent("search_contact", "details", target, source="deterministic")
        if requested_attribute != "none":
            return ContactsReadIntent("unknown", requested_attribute, None, ambiguous=True, reason="missing_target", source="deterministic")

    for pattern in _LIST_ALL_PATTERNS:
        if pattern.search(text):
            return ContactsReadIntent("list_contacts", "none", None, source="deterministic")

    if re.search(r"\bsearch\b", lowered) and re.search(r"\bcontacts?\b", lowered):
        target = _clean_target(text)
        if target:
            return ContactsReadIntent("search_contact", "details", target, source="deterministic")
        return ContactsReadIntent("list_contacts", "none", None, source="deterministic")

    return ContactsReadIntent("unknown", "none", None, ambiguous=True, reason="unrecognized_contacts_read", source="deterministic")


def _safe_json_loads(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            return json.loads(m.group(0))
        raise


def extract_contacts_read_intent_llm(message: str) -> ContactsReadExtraction | None:
    """LLM-powered structured extraction. Returns validated extraction or None.

    Never triggers Google API calls and never returns contact data/IDs.
    """
    system = (
        "You extract Google Contacts read intent. Return ONLY a single JSON object matching this schema:\n"
        '{"operation":"list_contacts|count_contacts|search_contact|get_contact_phone|get_contact_email|get_contact_details|unknown",'
        '"target":{"name":string|null,"email":string|null,"phone":string|null},'
        '"requested_attribute":"phone|email|details|all|none",'
        '"confidence":"low|medium|high",'
        '"ambiguity_reason":string|null,'
        '"requires_clarification":boolean}\n'
        "Rules: understand English, Bengali, and mixed-language requests. "
        "Identify list/count/search/specific-contact retrieval. Extract target name or explicitly provided search identifier. "
        "Identify requested attribute (phone/email/details/all/none). "
        "Recognize natural relationship references (e.g. hubby, wife, spouse, mom, dad, brother, sister) as lookup targets when the user is asking for an existing contact's details/phone/email; do not invent relationships or contact data. "
        "Distinguish asking for an existing contact's phone/email from unrelated mutation requests providing a new number. "
        "Return unknown or requires_clarification=true with ambiguity_reason when target/intent is ambiguous or missing for a specific lookup. "
        "Never invent a contact name or contact information. "
        "Never return Google resource names, contact IDs, connection IDs, or user IDs. "
        "Do not choose Google accounts. No extra fields."
    )
    request = LLMRequest(
        messages=[
            LLMMessage(role="system", content=system),
            LLMMessage(role="user", content=message),
        ],
        temperature=0.0,
        max_tokens=300,
    )
    try:
        resp = llm_router.generate(request)
        content = (resp.content or "").strip()
        if not content:
            return None
        data = _safe_json_loads(content)
        if not isinstance(data, dict):
            return None
        lowered_keys = {str(k).casefold() for k in data.keys()}
        if lowered_keys & {"resource_name", "resourcename", "contact_id", "connection_id", "user_id", "id"}:
            return None
        target = data.get("target")
        if isinstance(target, dict):
            tkeys = {str(k).casefold() for k in target.keys()}
            if tkeys & {"resource_name", "resourcename", "contact_id", "connection_id", "user_id", "id"}:
                return None
        return ContactsReadExtraction.model_validate(data)
    except Exception:
        return None


def _map_attr(attr: str) -> RequestedAttribute:
    if attr == "phone":
        return "phone"
    if attr == "email":
        return "email"
    if attr in {"details", "all"}:
        return "details"
    return "none"


def _extraction_to_intent(extraction: ContactsReadExtraction) -> ContactsReadIntent | None:
    op = extraction.operation
    attr = extraction.requested_attribute
    if op == "list_contacts":
        if attr not in {"none", "all", "details"}:
            return None
        return ContactsReadIntent("list_contacts", "none", None, ambiguous=False, source="llm")
    if op == "count_contacts":
        if attr not in {"none"}:
            return None
        return ContactsReadIntent("count_contacts", "none", None, ambiguous=False, source="llm")

    target_name = (extraction.target.name or "").strip() or None
    target_email = (extraction.target.email or "").strip() or None
    target_phone = (extraction.target.phone or "").strip() or None
    target_query = target_name or target_email or target_phone
    if target_query:
        target_query = re.sub(r"\s+", " ", target_query).strip()
        if len(target_query) > 120:
            target_query = target_query[:120].strip()
    if not target_query:
        return ContactsReadIntent("unknown", _map_attr(attr), None, ambiguous=True, reason=extraction.ambiguity_reason or "missing_target", source="llm")

    if extraction.requires_clarification or extraction.confidence == "low":
        return ContactsReadIntent("unknown", _map_attr(attr), target_query, ambiguous=True, reason=extraction.ambiguity_reason or "low_confidence", source="llm")

    if op == "search_contact":
        return ContactsReadIntent("search_contact", "details" if attr in {"all", "details"} else _map_attr(attr), target_query, source="llm")
    if op == "get_contact_phone":
        return ContactsReadIntent("get_contact_phone", "phone", target_query, source="llm")
    if op == "get_contact_email":
        return ContactsReadIntent("get_contact_email", "email", target_query, source="llm")
    if op == "get_contact_details":
        return ContactsReadIntent("get_contact_details", "details", target_query, source="llm")

    return ContactsReadIntent("unknown", _map_attr(attr), target_query, ambiguous=True, reason=extraction.ambiguity_reason or "unknown_intent", source="llm")


_GENERIC_RELATION_TARGETS = {
    "mom", "mother", "mum", "dad", "father", "parent", "parents",
    "brother", "sister", "sibling", "son", "daughter", "child", "children",
    "spouse", "husband", "wife", "partner", "boyfriend", "girlfriend",
}


def _llm_target_overgeneralized(message: str, llm_intent: ContactsReadIntent, det_intent: ContactsReadIntent) -> bool:
    """Avoid replacing a specific user-provided target with a generic relation label."""
    if not (llm_intent.target_query and det_intent.target_query):
        return False
    llm_t = _normalize_text(llm_intent.target_query)
    det_t = _normalize_text(det_intent.target_query)
    if not llm_t or not det_t or llm_t == det_t:
        return False
    if llm_t in _GENERIC_RELATION_TARGETS and det_t not in _GENERIC_RELATION_TARGETS:
        # Preserve specific name/nickname tokens (e.g. "mams", "rintus") from the request.
        if det_t in _normalize_text(message):
            return True
    return False


def resolve_contacts_read_intent(message: str) -> tuple[ContactsReadIntent, ContactsReadExtraction | None, str]:
    """Resolve intent: LLM structured extraction (validated) -> deterministic fallback -> clarify."""
    extraction = extract_contacts_read_intent_llm(message)
    if extraction is not None:
        intent = _extraction_to_intent(extraction)
        if intent is not None and not intent.ambiguous and intent.intent != "unknown":
            det = extract_contacts_read_intent(message)
            if (
                not det.ambiguous
                and det.intent != "unknown"
                and det.target_query
                and _llm_target_overgeneralized(message, intent, det)
            ):
                return det, extraction, "deterministic_target_preserved"
            # If deterministic strongly identifies same operation family and has a concrete target
            # while LLM omitted target in an unexpected way, prefer deterministic.
            return intent, extraction, "llm"
        if intent is not None and (intent.ambiguous or intent.intent == "unknown"):
            det = extract_contacts_read_intent(message)
            if not det.ambiguous and det.intent != "unknown" and det.target_query:
                return det, extraction, "deterministic_fallback"
            return intent, extraction, "llm_ambiguous"

    det = extract_contacts_read_intent(message)
    if not det.ambiguous and det.intent != "unknown":
        return det, extraction, "deterministic"
    return det, extraction, "needs_clarification"


def _normalize_text(s: str | None) -> str:
    if not isinstance(s, str):
        return ""
    normalized = re.sub(r"\s+", " ", s.casefold().strip())
    normalized = re.sub(r"[^\w@.+\-\s]", "", normalized, flags=re.UNICODE)
    return normalized.strip()


def _soft_name_token(token: str) -> str:
    """Conservative normalization for casual name variants (e.g. mams -> mam, rintus -> rintu).

    Used only as a secondary comparison signal to support reasonable partial
    matching. Never treated as an exact match and never used to silently select
    an ambiguous contact.
    """
    if not token:
        return ""
    t = token.strip().casefold()
    t = re.sub(r"(?:'s|’s)$", "", t)
    # Common casual/plural or affectionate suffixes: mams/mami-like variants are
    # normalized conservatively. Trailing possessive-like 's' in simple tokens
    # (e.g. rintus -> rintu) is handled as a soft variant only.
    if len(t) > 4 and t.endswith("s") and not t.endswith("ss"):
        t = t[:-1]
    if len(t) > 4 and t.endswith("i"):
        t = t[:-1]
    return t


def _tokens_soft_match(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    # Conservative near-match for name variants (mam/mams/mami, rintu/rintus-like soft forms).
    if len(a) >= 3 and len(b) >= 3 and (a.startswith(b) or b.startswith(a)) and abs(len(a) - len(b)) <= 2:
        return True
    return False


def _is_minor_name_typo(a: str, b: str) -> bool:
    """Conservative typo tolerance for local comparison against returned contact names only."""
    if not a or not b or a == b:
        return False
    la, lb = len(a), len(b)
    if min(la, lb) < 4 or abs(la - lb) > 1:
        return False
    # Single substitution
    if la == lb:
        diffs = sum(1 for i in range(la) if a[i] != b[i])
        return diffs == 1
    # Single insertion/deletion
    short, long = (a, b) if la < lb else (b, a)
    i = j = 0
    diff = 0
    while i < len(short) and j < len(long):
        if short[i] == long[j]:
            i += 1
            j += 1
        else:
            diff += 1
            j += 1
            if diff > 1:
                return False
    return True


def _normalize_phone(s: str | None) -> str:
    if not isinstance(s, str):
        return ""
    digits = re.sub(r"\D", "", s)
    return digits[-10:] if len(digits) >= 7 else digits


_RELATIONSHIP_SYNONYMS = {
    "hubby": {"spouse", "husband", "partner"},
    "husband": {"spouse", "husband", "partner", "hubby"},
    "wife": {"spouse", "wife", "partner"},
    "wifey": {"spouse", "wife", "partner"},
    "spouse": {"spouse", "husband", "wife", "partner", "hubby", "wifey"},
    "partner": {"spouse", "partner", "husband", "wife", "boyfriend", "girlfriend"},
    "boyfriend": {"boyfriend", "partner"},
    "girlfriend": {"girlfriend", "partner"},
    "mom": {"mother", "mom", "mum", "parent"},
    "mother": {"mother", "mom", "mum", "parent"},
    "dad": {"father", "dad", "parent"},
    "father": {"father", "dad", "parent"},
    "brother": {"brother", "sibling"},
    "sister": {"sister", "sibling"},
    "son": {"son", "child"},
    "daughter": {"daughter", "child"},
    "child": {"child", "son", "daughter"},
}


def _relationship_alias_terms(text: str) -> set[str]:
    t = _normalize_text(text)
    tokens = set(t.split())
    terms: set[str] = set()
    for token in list(tokens) + ([t] if t else []):
        terms.update(_RELATIONSHIP_SYNONYMS.get(token, set()))
    return {term for term in terms if term}


def _candidate_name_fields(contact: dict[str, Any]) -> list[str]:
    fields = [
        contact.get("display_name"),
        " ".join([x for x in [contact.get("given_name"), contact.get("family_name")] if isinstance(x, str) and x.strip()]),
        contact.get("given_name"),
        contact.get("family_name"),
        contact.get("organization"),
        contact.get("job_title"),
    ]
    for item in (contact.get("nicknames") or []):
        if isinstance(item, dict) and isinstance(item.get("value"), str):
            fields.append(item.get("value"))
    for item in (contact.get("relations") or []):
        if not isinstance(item, dict):
            continue
        # Relation person/value can be a name in some APIs; include grounded values/types.
        for key in ("value", "person", "type", "formatted_type"):
            val = item.get(key)
            if isinstance(val, str):
                fields.append(val)
    for item in (contact.get("user_defined") or []):
        if not isinstance(item, dict):
            continue
        for key in ("key", "value"):
            val = item.get(key)
            if isinstance(val, str):
                fields.append(val)
    result: list[str] = []
    for f in fields:
        if isinstance(f, str) and f.strip():
            result.append(f)
    return result


def _candidate_values(contact: dict[str, Any]) -> tuple[list[str], list[str]]:
    phones = [p.get("value") for p in (contact.get("phones") or []) if isinstance(p, dict) and isinstance(p.get("value"), str)]
    emails = [e.get("value") for e in (contact.get("emails") or []) if isinstance(e, dict) and isinstance(e.get("value"), str)]
    if contact.get("primary_phone") and contact.get("primary_phone") not in phones:
        phones.insert(0, contact.get("primary_phone"))
    if contact.get("primary_email") and contact.get("primary_email") not in emails:
        emails.insert(0, contact.get("primary_email"))
    return phones, emails


def _name_match_bucket(target: str, contact: dict[str, Any]) -> tuple[int, bool]:
    """Return (strength_score, is_exact_name). Never treats partial/fuzzy as exact."""
    t = _normalize_text(target)
    if not t:
        return 0, False
    score = 0
    is_exact = False
    relation_alias_terms = _relationship_alias_terms(target)
    # Grounded relationship matching: only when the requested term maps to known relationship
    # synonyms and the contact's relation/nickname/userDefined metadata explicitly contains them.
    if relation_alias_terms:
        grounded_relation_values: list[str] = []
        for item in (contact.get("relations") or []):
            if not isinstance(item, dict):
                continue
            for key in ("type", "formatted_type", "value", "person"):
                val = item.get(key)
                if isinstance(val, str) and val.strip():
                    grounded_relation_values.append(val)
        for item in (contact.get("nicknames") or []):
            if isinstance(item, dict) and isinstance(item.get("value"), str):
                grounded_relation_values.append(item["value"])
        for item in (contact.get("user_defined") or []):
            if not isinstance(item, dict):
                continue
            for key in ("key", "value"):
                val = item.get(key)
                if isinstance(val, str) and val.strip():
                    grounded_relation_values.append(val)
        for value in grounded_relation_values:
            nv = _normalize_text(value)
            if not nv:
                continue
            nv_tokens = set(nv.split())
            if nv in relation_alias_terms or nv_tokens & relation_alias_terms:
                # Grounded relationship/alias evidence from actual contact metadata.
                score = max(score, 92)
    for f in _candidate_name_fields(contact):
        n = _normalize_text(f)
        if not n:
            continue
        if n == t:
            score = max(score, 100)
            is_exact = True
        elif t in n or n in t:
            score = max(score, 70)
        else:
            t_tokens = set(t.split())
            n_tokens = set(n.split())
            if t_tokens and t_tokens.issubset(n_tokens):
                score = max(score, 65)
            elif t_tokens and n_tokens and (t_tokens & n_tokens):
                overlap_tokens = t_tokens & n_tokens
                meaningful_overlap = {x for x in overlap_tokens if len(x) >= 3}
                if t_tokens.issubset(n_tokens):
                    score = max(score, 65)
                elif len(meaningful_overlap) >= 1:
                    # Require at least one meaningful token overlap to avoid weak suffix matches.
                    if overlap_tokens == t_tokens and len(t_tokens) >= 2:
                        score = max(score, 60)
                    else:
                        score = max(score, 40)
                # else: only short/common tokens overlap (e.g., "di") -> ignore
            # Conservative soft-token comparison for common casual variants
            # (e.g. "mams" -> "mam", "rintus" -> "rintu"). Partial strength only; never exact and
            # never a basis for silent auto-selection.
            t_soft = {_soft_name_token(tok) for tok in t.split() if _soft_name_token(tok)}
            n_soft = {_soft_name_token(tok) for tok in n.split() if _soft_name_token(tok)}
            if t_soft and n_soft and score < 80:
                if t_soft == n_soft:
                    score = max(score, 78)
                elif t_soft.issubset(n_soft) or n_soft.issubset(t_soft):
                    score = max(score, 72)
                elif all(any(_tokens_soft_match(ts, ns) or (len(ts) >= 3 and len(ns) >= 3 and (ts in ns or ns in ts)) for ns in n_soft) for ts in t_soft):
                    score = max(score, 68)
                # Minor typo tolerance for single-token names (conservative, local comparison over returned contacts only).
                elif len(t_soft) == 1 and len(n_soft) == 1:
                    ts = next(iter(t_soft))
                    ns = next(iter(n_soft))
                    if _is_minor_name_typo(ts, ns):
                        score = max(score, 70)

    phones, emails = _candidate_values(contact)
    if "@" in t:
        for e in emails:
            ne = _normalize_text(e)
            if ne == t:
                score = max(score, 100)
                is_exact = True
            elif t in ne or ne in t:
                score = max(score, 70)
    t_phone = _normalize_phone(target)
    if t_phone and len(t_phone) >= 7:
        for p in phones:
            np = _normalize_phone(p)
            if not np:
                continue
            if np == t_phone or (len(t_phone) >= 7 and (np.endswith(t_phone) or t_phone.endswith(np))):
                score = max(score, 100)
                is_exact = True
            elif t_phone in np or np in t_phone:
                score = max(score, 70)

    return score, is_exact


def _minimal_contact(contact: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in (
        "resource_name",
        "display_name",
        "given_name",
        "family_name",
        "primary_phone",
        "primary_email",
        "organization",
        "job_title",
    ):
        value = contact.get(key)
        if value not in (None, "", [], {}):
            out[key] = value
    phones = contact.get("phones") if isinstance(contact.get("phones"), list) else []
    emails = contact.get("emails") if isinstance(contact.get("emails"), list) else []
    if phones:
        cleaned = []
        for p in phones:
            if not isinstance(p, dict) or not p.get("value"):
                continue
            item = {k: v for k, v in p.items() if k in {"value", "type", "primary"} and v not in (None, "")}
            if item:
                cleaned.append(item)
        if cleaned:
            out["phones"] = cleaned
    if emails:
        cleaned = []
        for e in emails:
            if not isinstance(e, dict) or not e.get("value"):
                continue
            item = {k: v for k, v in e.items() if k in {"value", "type", "primary"} and v not in (None, "")}
            if item:
                cleaned.append(item)
        if cleaned:
            out["emails"] = cleaned
    addresses = contact.get("addresses") if isinstance(contact.get("addresses"), list) else []
    if addresses:
        cleaned_addrs = []
        for a in addresses:
            if not isinstance(a, dict):
                continue
            item = {k: v for k, v in a.items() if k in {"formatted", "type", "primary", "street", "city", "region", "postal_code", "country"} and v not in (None, "")}
            if item:
                cleaned_addrs.append(item)
        if cleaned_addrs:
            out["addresses"] = cleaned_addrs
    return out


def _candidate_summary(contact: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    rn = contact.get("resource_name")
    if isinstance(rn, str) and rn:
        summary["resource_name"] = rn  # trusted server-side context only
    for key in ("display_name", "given_name", "family_name", "organization", "job_title"):
        value = contact.get(key)
        if isinstance(value, str) and value.strip():
            summary[key] = value.strip()
    return summary


def _clarification_candidates_for_prompt(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Candidates exposed in clarification (names/identifying labels only, no phone/email)."""
    safe = []
    for c in candidates:
        if not isinstance(c, dict):
            continue
        item = {}
        for k in ("display_name", "given_name", "family_name", "organization", "job_title"):
            if k in c and c[k] not in (None, "", []):
                item[k] = c[k]
        if item:
            safe.append(item)
    return safe[:5]


def _display_name(contact: dict[str, Any]) -> str:
    for key in ("display_name",):
        v = contact.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    parts = [p for p in [contact.get("given_name"), contact.get("family_name")] if isinstance(p, str) and p.strip()]
    if parts:
        return " ".join(parts).strip()
    return "Unnamed contact"


def _contact_has_phone(contact: dict[str, Any]) -> bool:
    phones = contact.get("phones") if isinstance(contact.get("phones"), list) else []
    return bool(contact.get("primary_phone") or any(isinstance(p, dict) and p.get("value") for p in phones))


def _contact_has_email(contact: dict[str, Any]) -> bool:
    emails = contact.get("emails") if isinstance(contact.get("emails"), list) else []
    return bool(contact.get("primary_email") or any(isinstance(e, dict) and e.get("value") for e in emails))


def _format_field_values(contact: dict[str, Any], attribute: RequestedAttribute) -> str | None:
    if attribute == "phone":
        phones = [p.get("value") for p in (contact.get("phones") or []) if isinstance(p, dict) and isinstance(p.get("value"), str) and p.get("value").strip()]
        if contact.get("primary_phone") and contact.get("primary_phone") not in phones:
            phones.insert(0, contact.get("primary_phone"))
        uniq = list(dict.fromkeys([p.strip() for p in phones if isinstance(p, str) and p.strip()]))
        return ", ".join(uniq) if uniq else None
    if attribute == "email":
        emails = [e.get("value") for e in (contact.get("emails") or []) if isinstance(e, dict) and isinstance(e.get("value"), str) and e.get("value").strip()]
        if contact.get("primary_email") and contact.get("primary_email") not in emails:
            emails.insert(0, contact.get("primary_email"))
        uniq = list(dict.fromkeys([e.strip() for e in emails if isinstance(e, str) and e.strip()]))
        return ", ".join(uniq) if uniq else None
    return None


_SUSPICIOUS_EMAIL_DOMAINS = frozenset({
    "example.com",
    "example.org",
    "example.net",
    "example.edu",
    "test.com",
    "test.org",
    "invalid",
    "localhost",
})
_SUSPICIOUS_ORG_MARKERS = (
    "xyz corp",
    "example corp",
    "acme corp",
    "test corp",
    "fake company",
)
_SUSPICIOUS_ADDRESS_MARKERS = (
    "123 main st",
    "springfield",
    "anytown",
    "123 example",
)
# Common reserved/fake phone exchanges (555-0100..0199, 555-1234, 555-9876).
_FAKE_PHONE_DIGITS = re.compile(r"555(?:01\d{2}|123|987)")


def _contact_values_for_field(
    contact: dict[str, Any],
    field: Literal["email", "phone", "organization", "address"],
) -> list[str]:
    """Return user-visible values for one field, without logging or exposing them."""
    values: list[str] = []
    if field == "email":
        primary = contact.get("primary_email")
        if isinstance(primary, str) and primary.strip():
            values.append(primary.strip())
        for item in contact.get("emails") or []:
            if isinstance(item, dict):
                value = item.get("value")
                if isinstance(value, str) and value.strip():
                    values.append(value.strip())
    elif field == "phone":
        primary = contact.get("primary_phone")
        if isinstance(primary, str) and primary.strip():
            values.append(primary.strip())
        for item in contact.get("phones") or []:
            if isinstance(item, dict):
                value = item.get("value")
                if isinstance(value, str) and value.strip():
                    values.append(value.strip())
    elif field == "organization":
        value = contact.get("organization")
        if isinstance(value, str) and value.strip():
            values.append(value.strip())
    elif field == "address":
        for item in contact.get("addresses") or []:
            if not isinstance(item, dict):
                continue
            formatted = item.get("formatted")
            if isinstance(formatted, str) and formatted.strip():
                values.append(formatted.strip())
                continue
            parts = [item.get(key) for key in ("street", "city", "region", "postal_code", "country")]
            text = ", ".join([str(part).strip() for part in parts if isinstance(part, str) and part.strip()])
            if text:
                values.append(text)
    # Deduplicate while preserving order.
    return list(dict.fromkeys(values))


def _suspicious_contact_patterns(contact: dict[str, Any]) -> list[tuple[str, str]]:
    """Detect obvious test/fake data patterns without retaining raw values.

    Returns only (field_identifier, pattern_category) pairs so callers can log
    safely. Raw phone numbers, emails, addresses and names are never returned
    or logged by this helper.
    """
    findings: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(field: str, pattern: str) -> None:
        key = (field, pattern)
        if key not in seen:
            seen.add(key)
            findings.append(key)

    try:
        for value in _contact_values_for_field(contact, "email"):
            _local, _sep, domain = value.rpartition("@")
            domain = domain.strip().casefold()
            if not domain:
                continue
            if (
                domain in _SUSPICIOUS_EMAIL_DOMAINS
                or domain.endswith(".invalid")
                or domain.endswith(".test")
            ):
                add("email", "example_or_test_domain")

        for value in _contact_values_for_field(contact, "phone"):
            digits = re.sub(r"\D", "", value)
            if _FAKE_PHONE_DIGITS.search(digits) or "(555)" in value:
                add("phone", "555_test_number")

        for value in _contact_values_for_field(contact, "organization"):
            lowered = value.casefold()
            if any(marker in lowered for marker in _SUSPICIOUS_ORG_MARKERS):
                add("organization", "test_org")

        for value in _contact_values_for_field(contact, "address"):
            lowered = value.casefold()
            if any(marker in lowered for marker in _SUSPICIOUS_ADDRESS_MARKERS):
                add("address", "test_address")
    except Exception:
        # Defensive checks must never break rendering.
        return findings

    return findings


def _log_suspicious_contact_patterns(contact: dict[str, Any]) -> None:
    """Log only safe field identifiers and pattern categories (never raw values)."""
    for field, pattern in _suspicious_contact_patterns(contact):
        logger.warning(
            "contacts_suspicious_data_detected | field=%s | pattern=%s",
            field,
            pattern,
        )


def _format_contact_details(contact: dict[str, Any]) -> str:
    name = _display_name(contact)
    lines = [f"Contact details for {name}:"]
    phones = _format_field_values(contact, "phone")
    emails = _format_field_values(contact, "email")
    if phones:
        lines.append(f"- Phone: {phones}")
    else:
        lines.append("- Phone: Not available")
    if emails:
        lines.append(f"- Email: {emails}")
    else:
        lines.append("- Email: Not available")
    org = contact.get("organization")
    job = contact.get("job_title")
    if isinstance(org, str) and org.strip():
        lines.append(f"- Organization: {org.strip()}")
    if isinstance(job, str) and job.strip():
        lines.append(f"- Job title: {job.strip()}")
    addrs = contact.get("addresses") if isinstance(contact.get("addresses"), list) else []
    formatted_addrs = []
    for a in addrs:
        if not isinstance(a, dict):
            continue
        if isinstance(a.get("formatted"), str) and a.get("formatted").strip():
            formatted_addrs.append(a.get("formatted").strip())
        else:
            parts = [a.get(k) for k in ("street", "city", "region", "postal_code", "country")]
            text = ", ".join([str(p).strip() for p in parts if isinstance(p, str) and p.strip()])
            if text:
                formatted_addrs.append(text)
    if formatted_addrs:
        lines.append(f"- Address: {'; '.join(list(dict.fromkeys(formatted_addrs)))}")
    else:
        lines.append("- Address: Not available")
    return "\n".join(lines)


def _classify_search_results(*, intent: ContactsReadIntent, contacts: list[dict[str, Any]]) -> dict[str, Any]:
    valid: list[dict[str, Any]] = []
    for c in contacts:
        if not isinstance(c, dict):
            continue
        rn = c.get("resource_name")
        if isinstance(rn, str) and _RESOURCE_NAME_RE.match(rn.strip()):
            valid.append(c)

    target = intent.target_query or ""
    outcome: dict[str, Any] = {
        "kind": intent.intent,
        "requested_attribute": intent.requested_attribute,
        "target_query": intent.target_query,
        "data_source": "google_people_api",
        "selected_contact_returned_by_api": False,
        "match_strategy": "search_contacts",
    }

    if not valid:
        outcome["matches"] = []
        outcome["match_count"] = 0
        outcome["status"] = "no_match"
        outcome["match_strategy"] = "no_match"
        return outcome

    exact_matches: list[dict[str, Any]] = []
    partial_matches: list[tuple[int, dict[str, Any]]] = []
    for c in valid:
        score, is_exact = _name_match_bucket(target, c) if target else (100, True)
        if target and is_exact and score >= 100:
            exact_matches.append(c)
        elif target and score >= 60:
            partial_matches.append((score, c))
        elif not target:
            partial_matches.append((100, c))

    # Determine candidate pool (conservative: do not silently pick among partials).
    if len(exact_matches) >= 1:
        pool = list(exact_matches)
        base_strategy = "single_exact" if len(exact_matches) == 1 else "multiple_exact"
    elif exact_matches == [] and partial_matches:
        partial_matches.sort(key=lambda x: (-x[0], _display_name(x[1]).casefold()))
        pool = [c for _, c in partial_matches]
        base_strategy = "single_partial_confirmation" if len(pool) == 1 else "multiple_partial_confirmation"
    else:
        outcome["matches"] = []
        outcome["match_count"] = 0
        outcome["status"] = "no_match"
        outcome["match_strategy"] = "no_match"
        return outcome

    # Final eligible set for ambiguity/display/storage: valid, deduped by resource_name, ordered, bounded.
    seen_rns: set[str] = set()
    eligible: list[dict[str, Any]] = []
    for c in pool:
        if not isinstance(c, dict):
            continue
        rn = c.get("resource_name")
        rn_key = rn.strip() if isinstance(rn, str) else ""
        if rn_key and rn_key in seen_rns:
            continue
        if rn_key:
            seen_rns.add(rn_key)
        eligible.append(c)
        if len(eligible) >= 5:
            break

    if len(eligible) == 0:
        outcome["matches"] = []
        outcome["match_count"] = 0
        outcome["status"] = "no_match"
        outcome["match_strategy"] = "no_match"
        return outcome

    if len(eligible) == 1:
        # A single partial/non-exact candidate still requires confirmation (no silent fuzzy selection),
        # except when an exact-match path already produced one definitive match.
        if len(exact_matches) == 0:
            selected = eligible
            status = "multiple_matches"
            match_strategy = "single_partial_confirmation"
        else:
            selected = eligible
            status = "single_match"
            match_strategy = base_strategy if "single" in base_strategy else "single_exact"
    else:
        selected = eligible
        status = "multiple_matches"
        match_strategy = base_strategy if "multiple" in base_strategy else "multiple_partial_confirmation"

    outcome["matches"] = [_minimal_contact(c) for c in selected]
    outcome["match_count"] = len(selected)
    outcome["status"] = status
    outcome["match_strategy"] = match_strategy

    if status == "multiple_matches":
        summaries = [_candidate_summary(c) for c in selected]
        display_candidates: list[dict[str, Any]] = []
        for s in summaries:
            item = _clarification_candidates_for_prompt([s])[0] if _clarification_candidates_for_prompt([s]) else {}
            if not item:
                name = (s.get("display_name") or s.get("given_name") or "Unnamed contact")
                name = name.strip() if isinstance(name, str) else "Unnamed contact"
                item = {"display_name": name or "Unnamed contact"}
            display_candidates.append(item)
        outcome["candidate_summaries"] = summaries
        outcome["clarification_candidates"] = display_candidates
        outcome["exact_match_count"] = len(exact_matches)
        outcome["partial_match_count"] = 0 if exact_matches else len(pool)
        return outcome

    outcome["selected_contact_returned_by_api"] = True
    contact = selected[0]
    attr = intent.requested_attribute
    if attr == "phone":
        outcome["field_status"] = "present" if _contact_has_phone(contact) else "missing"
    elif attr == "email":
        outcome["field_status"] = "present" if _contact_has_email(contact) else "missing"
    else:
        outcome["field_status"] = "present"
    return outcome


_ALL_MATCHES_PHONE_RE = re.compile(
    r"\b(?:all|every|both)\b.{0,60}\b(?:phone|mobile|contact)?\s*numbers?\b|"
    r"\bphone\s+numbers\b|"
    r"\b(?:numbers?|phone\s+numbers?|mobile\s+numbers?|contact\s+numbers?)\b.{0,40}\b(?:associated\s+with|for|of|belonging\s+to)\b|"
    r"\b(?:associated\s+with|for|of)\b.{0,40}\b(?:phone\s+numbers?|numbers?)\b",
    re.IGNORECASE,
)
_ALL_MATCHES_EMAIL_RE = re.compile(
    r"\b(?:all|every|both)\b.{0,60}\b(?:email|e-mail|mail)\b|"
    r"\b(?:email\s+addresses?|e-mail\s+addresses?|emails?)\b.{0,40}\b(?:associated\s+with|for|of|belonging\s+to)\b|"
    r"\b(?:associated\s+with|for|of)\b.{0,40}\b(?:email\s+addresses?|emails?)\b",
    re.IGNORECASE,
)
_ALL_MATCHES_DETAILS_RE = re.compile(
    r"\b(?:all|every|both)\b.{0,60}\b(?:contact\s+details|details|contacts?)\b|"
    r"\bdetails\b.{0,40}\b(?:associated\s+with|for|of|belonging\s+to)\b|"
    r"\b(?:associated\s+with|for|of)\b.{0,40}\b(?:details|contact\s+details)\b",
    re.IGNORECASE,
)


def _requests_all_matching_contacts(message: str, requested_attribute: RequestedAttribute) -> bool:
    """True when user explicitly asks for the requested values across all matching contacts."""
    text = (message or "").strip()
    if not text:
        return False
    if requested_attribute == "phone":
        return bool(_ALL_MATCHES_PHONE_RE.search(text))
    if requested_attribute == "email":
        return bool(_ALL_MATCHES_EMAIL_RE.search(text))
    if requested_attribute == "details":
        return bool(_ALL_MATCHES_DETAILS_RE.search(text))
    return bool(
        _ALL_MATCHES_PHONE_RE.search(text)
        or _ALL_MATCHES_EMAIL_RE.search(text)
        or _ALL_MATCHES_DETAILS_RE.search(text)
    )


def _render_trusted_response(outcome: dict[str, Any]) -> str:
    """Render user-facing Contacts response from verified backend data only.

    Never invent or include any phone/email not present in trusted outcome matches.
    """
    status = outcome.get("status")
    attr = outcome.get("requested_attribute")
    target = outcome.get("target_query")

    # Defensive logging: inspect only contacts that may be rendered, and log
    # safe field identifiers + pattern categories (never raw values).
    if status in {"single_match", "multiple_matches_all", "list"}:
        render_candidates = (
            outcome.get("contacts")
            if status == "list"
            else outcome.get("matches")
        )
        for candidate in render_candidates or []:
            if isinstance(candidate, dict):
                _log_suspicious_contact_patterns(candidate)

    if status == "no_match":
        return (
            f'I couldn\'t find a contact matching "{target}" in your Google Contacts.'
            if target
            else "I couldn't find a matching contact in your Google Contacts."
        )

    if status == "multiple_matches_all":
        if outcome.get("data_source") != "google_people_api" or not outcome.get("selected_contact_returned_by_api"):
            return "I couldn't verify the contact details from Google Contacts for this request. Please try again."
        matches = [m for m in (outcome.get("matches") or []) if isinstance(m, dict)]
        failures = outcome.get("partial_failures") or []
        if not matches and not failures:
            return "I couldn't find matching contacts in your Google Contacts."
        if attr == "phone":
            header = (
                f'Phone numbers for contacts matching "{target}":'
                if target else "Phone numbers for matching contacts:"
            )
            lines = [header]
            for m in matches[:10]:
                name = _display_name(m)
                value = _format_field_values(m, "phone")
                if value:
                    lines.append(f"- {name}: {value}")
                else:
                    lines.append(f"- {name}: No phone number available")
            if failures:
                names = ", ".join([f.get("display_name") or "A contact" for f in failures[:5]])
                lines.append(f"Could not retrieve details for: {names}.")
            if not matches and failures:
                return "Could not retrieve the requested contact details right now."
            return "\n".join(lines)
        if attr == "email":
            header = (
                f'Email addresses for contacts matching "{target}":'
                if target else "Email addresses for matching contacts:"
            )
            lines = [header]
            for m in matches[:10]:
                name = _display_name(m)
                value = _format_field_values(m, "email")
                if value:
                    lines.append(f"- {name}: {value}")
                else:
                    lines.append(f"- {name}: No email address available")
            if failures:
                names = ", ".join([f.get("display_name") or "A contact" for f in failures[:5]])
                lines.append(f"Could not retrieve details for: {names}.")
            if not matches and failures:
                return "Could not retrieve the requested contact details right now."
            return "\n".join(lines)
        header = (
            f'Contact details for contacts matching "{target}":'
            if target else "Contact details for matching contacts:"
        )
        blocks = [header]
        for m in matches[:10]:
            blocks.append(_format_contact_details(m))
        if failures:
            names = ", ".join([f.get("display_name") or "A contact" for f in failures[:5]])
            blocks.append(f"Could not retrieve details for: {names}.")
        if not matches and failures:
            return "Could not retrieve the requested contact details right now."
        return "\n\n".join(blocks)

    if status == "multiple_matches":
        cands = outcome.get("clarification_candidates") or []
        names = []
        for c in cands:
            if not isinstance(c, dict):
                continue
            names.append(_display_name(c))
        if not names:
            names = [_display_name(m) for m in (outcome.get("matches") or []) if isinstance(m, dict)]
        if names:
            lines = "\n".join(f"{i}. {name}" for i, name in enumerate(names[:5], start=1))
            if target:
                prefix = f'I found multiple contacts matching "{target}":\n'
            else:
                prefix = "I found multiple matching contacts:\n"
            if outcome.get("exact_match_count", 0) and outcome.get("exact_match_count", 0) > 1:
                note = "Which contact do you mean? Please reply with the number or the exact contact name."
            else:
                note = "I need you to confirm which contact you mean. Please reply with the number or the exact contact name."
            return prefix + lines + "\n" + note
        if target:
            return f'I found multiple contacts matching "{target}". Which contact do you mean? Please reply with the number or the exact contact name.'
        return "I found multiple matching contacts. Which contact do you mean? Please reply with the number or the exact contact name."

    if status == "single_match":
        # Mandatory safety: display details only when traceable to validated Google People API data for this request.
        if outcome.get("data_source") != "google_people_api" or not outcome.get("selected_contact_returned_by_api"):
            return "I couldn't verify the contact details from Google Contacts for this request. Please try again."
        match = (outcome.get("matches") or [None])[0] or {}
        name = _display_name(match)
        if attr == "phone":
            if outcome.get("field_status") == "missing":
                return f"{name} does not have a phone number available in Google Contacts."
            value = _format_field_values(match, "phone")
            if not value:
                return f"{name} does not have a phone number available in Google Contacts."
            return f"{name}'s phone number: {value}"
        if attr == "email":
            if outcome.get("field_status") == "missing":
                return f"{name} does not have an email address available in Google Contacts."
            value = _format_field_values(match, "email")
            if not value:
                return f"{name} does not have an email address available in Google Contacts."
            return f"{name}'s email address: {value}"
        return _format_contact_details(match)

    if status == "list":
        contacts = outcome.get("contacts") or []
        n = outcome.get("returned_count", len(contacts))
        total = outcome.get("total")
        if not contacts:
            return "I couldn't find any contacts to show from your Google Contacts."
        lines = []
        for c in contacts[:30]:
            if not isinstance(c, dict):
                continue
            name = _display_name(c)
            parts = [name]
            if _contact_has_phone(c):
                parts.append("phone available")
            if _contact_has_email(c):
                parts.append("email available")
            lines.append(f"- {name}" + (f" ({', '.join(parts[1:])})" if len(parts) > 1 else ""))
        if total is not None and total != n:
            header = f"Showing {n} of {total} contact(s) from your Google Contacts:"
        else:
            header = f"Showing {n} contact(s) from your Google Contacts:"
        suffix = "\nMore contacts are available." if outcome.get("has_more") else ""
        return header + "\n" + "\n".join(lines) + suffix

    if status == "count":
        total = outcome.get("total")
        returned = outcome.get("returned_count", 0)
        if total is not None:
            suffix = " (there may be more contacts not included in this count)." if outcome.get("has_more") else ""
            return f"You have {total} contact(s) in Google Contacts.{suffix}"
        if outcome.get("has_more"):
            return f"I found at least {returned} contact(s) in Google Contacts."
        return f"I found {returned} contact(s) in Google Contacts."

    if status == "incomplete_search":
        return outcome.get("message") or (
            "I searched part of your Google Contacts but couldn’t confidently find a matching contact "
            "within the search limit. Please try a more specific name, email, or phone number."
        )

    if status == "needs_clarification":
        return outcome.get("message") or "Please tell me which contact you want me to look up, or ask to list your contacts."

    if status == "error":
        return outcome.get("error") or "I couldn't retrieve your Google Contacts right now. Please try again."

    return "I couldn't complete the Contacts request right now. Please try again."


def _is_valid_pending_contacts_clarification(
    pending: Any, *, user_id: str, conversation_id: str, now: datetime
) -> bool:
    if not isinstance(pending, dict):
        return False
    if pending.get("version") != 1 or pending.get("type") != "contacts_read_clarification":
        return False
    if str(pending.get("user_id")) != str(user_id) or str(pending.get("conversation_id")) != str(conversation_id):
        return False
    try:
        expires_at = datetime.fromisoformat(str(pending.get("expires_at")).replace("Z", "+00:00"))
    except Exception:
        return False
    if expires_at.tzinfo is None or expires_at.astimezone(timezone.utc) <= now.astimezone(timezone.utc):
        return False
    cands = pending.get("candidates")
    if not isinstance(cands, list) or not cands:
        return False
    for c in cands:
        if not isinstance(c, dict):
            return False
        rn = c.get("resource_name")
        if not isinstance(rn, str) or not _RESOURCE_NAME_RE.match(rn):
            return False
    return True


def _resolve_clarification_selection(message: str, candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Resolve follow-up against stored trusted candidates only. No client IDs accepted."""
    text = (message or "").strip()
    lowered = text.casefold()
    if not text or not candidates:
        return None

    # Numeric/ordinal selection (tolerant)
    m = re.fullmatch(r"\s*(?:(?:option|number|choice|select)\s*)?([1-9][0-9]?)\s*[\).:!\-]?\s*", lowered)
    if m:
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(candidates):
            return candidates[idx]
    # Also handle "the 2nd one", "2nd"
    m2 = re.search(r"\b(\d+)\s*(?:st|nd|rd|th)?\b", lowered)
    if m2 and re.fullmatch(r"\s*(?:the\s+)?\d+\s*(?:st|nd|rd|th)?(?:\s*(?:one|contact|option|number|choice))?\s*", lowered):
        try:
            idx = int(m2.group(1)) - 1
            if 0 <= idx < len(candidates):
                return candidates[idx]
        except Exception:
            pass
    ordinals = {
        "first": 0, "1st": 0, "the first one": 0, "first one": 0, "first contact": 0,
        "second": 1, "2nd": 1, "the second one": 1, "second one": 1, "second contact": 1,
        "third": 2, "3rd": 2, "fourth": 3, "4th": 3, "fifth": 4, "5th": 4,
    }
    for key, idx in ordinals.items():
        if lowered == key and idx < len(candidates):
            return candidates[idx]

    scores = []
    for idx, c in enumerate(candidates):
        score = 0
        dn = _normalize_text(c.get("display_name"))
        gn = _normalize_text(c.get("given_name"))
        fn = _normalize_text(c.get("family_name"))
        full = _normalize_text(" ".join([x for x in [c.get("given_name"), c.get("family_name")] if isinstance(x, str)]))
        org = _normalize_text(c.get("organization"))
        for cand_name in [dn, full, gn, fn]:
            if cand_name and cand_name == lowered:
                score = max(score, 100)
            elif cand_name and (cand_name in lowered or lowered in cand_name):
                score = max(score, 80)
        tokens = set(re.findall(r"[^\W_]+", lowered, flags=re.UNICODE))
        cand_tokens = set(re.findall(r"[^\W_]+", " ".join([x for x in [dn, full, org] if x]), flags=re.UNICODE))
        if tokens and cand_tokens and (tokens & cand_tokens):
            score = max(score, 65)
        scores.append((score, idx, c))
    scores.sort(key=lambda x: (-x[0], x[1]))
    if not scores:
        return None
    best_score, _, best = scores[0]
    if best_score >= 80:
        top = [c for s, _, c in scores if s == best_score]
        if len(top) == 1:
            return best
    if best_score == 100:
        return best
    return None


def _is_attribute_followup_without_selection(message: str, pending: dict[str, Any]) -> bool:
    attr = _detect_requested_attribute(message)
    if attr == "none":
        return False
    if _SELECTION_ONLY_RE.match((message or "").strip()):
        return False
    # Pronoun/reference to previously discussed contact while clarification unresolved.
    if _PRONOUN_TARGET_RE.search((message or "").strip()):
        return True
    target = (pending.get("target_query") or "").strip().casefold()
    lowered = (message or "").casefold()
    if target and target in lowered:
        return True
    # Generic "show me the number/email" without identifying which candidate.
    cleaned = _clean_target(message or "")
    return cleaned is None


_CORRECTION_REJECT_PATTERNS = [
    re.compile(r"\b(?:not|no,?\s*not|don['’]?t\s+want|i\s+don['’]?t\s+want|not\s+that\s+one|not\s+that\s+contact)\b", re.IGNORECASE),
    re.compile(r"\b(?:no,?\s*the\s+other|other\s+one|different\s+one|someone\s+else)\b", re.IGNORECASE),
]
_CORRECTION_TARGET_PATTERNS = [
    re.compile(r"\b(?:i\s+want|i\s+meant|i\s+mean|show\s+me|give\s+me|looking\s+for|need)\b\s+(?:the\s+)?(?:contact\s+(?:named|called)\s+)?(?P<target>[^\n,.!?]+)", re.IGNORECASE),
    re.compile(r"\b(?:the\s+other|another)\s+(?P<target>[A-Za-z0-9\u0980-\u09FF][A-Za-z0-9\u0980-\u09FF .'\-]{0,80})", re.IGNORECASE),
]


def _match_candidate_by_name_hint(hint: str, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not hint or not candidates:
        return []
    norm_hint = _normalize_text(hint)
    if not norm_hint:
        return []
    hint_tokens = [t for t in norm_hint.split() if t]
    hint_soft = {_soft_name_token(t) for t in hint_tokens if _soft_name_token(t)}
    matched: list[tuple[int, dict[str, Any]]] = []
    for c in candidates:
        if not isinstance(c, dict):
            continue
        names = [c.get("display_name"), c.get("given_name"), c.get("family_name")]
        combined = " ".join([n for n in names if isinstance(n, str) and n.strip()])
        fields = [n for n in names if isinstance(n, str) and n.strip()] + ([combined] if combined.strip() else [])
        score = 0
        for f in fields:
            nf = _normalize_text(f)
            if not nf:
                continue
            nf_tokens = [t for t in nf.split() if t]
            nf_soft = {_soft_name_token(t) for t in nf_tokens if _soft_name_token(t)}
            if nf == norm_hint:
                score = max(score, 100)
                continue
            # Strong containment: full hint appears as words inside a longer candidate name (or vice versa) when multi-token.
            if len(hint_tokens) >= 2 and (f" {norm_hint} " in f" {nf} " or norm_hint == nf):
                score = max(score, 92)
            elif len(nf_tokens) >= 2 and f" {nf} " in f" {norm_hint} ":
                score = max(score, 88)
            elif norm_hint in nf and len(hint_tokens) >= 2:
                score = max(score, 85)
            elif nf in norm_hint and len(nf_tokens) >= 2:
                score = max(score, 85)
            # Token-overlap evidence (preferred for distinguishing similar short names).
            if hint_soft and nf_soft:
                inter = hint_soft & nf_soft
                soft_pairs = sum(1 for ht in hint_soft for nt in nf_soft if _tokens_soft_match(ht, nt))
                strong_pair_overlap = sum(1 for ht in hint_soft if any(_tokens_soft_match(ht, nt) for nt in nf_soft))
                strong_nf_overlap = sum(1 for nt in nf_soft if any(_tokens_soft_match(nt, ht) for ht in hint_soft))
                if hint_soft == nf_soft and hint_soft:
                    score = max(score, 95)
                elif inter and inter == hint_soft and len(hint_soft) >= 2:
                    score = max(score, 90)
                elif strong_pair_overlap == len(hint_soft) and len(hint_soft) >= 2:
                    score = max(score, 88)
                elif inter and inter == nf_soft and len(nf_soft) >= 2:
                    score = max(score, 82)
                elif strong_nf_overlap == len(nf_soft) and len(nf_soft) >= 2:
                    score = max(score, 82)
                elif len(inter) >= 2 or soft_pairs >= 2:
                    score = max(score, 78)
                elif len(inter) == 1 and len(hint_soft) == 1 and len(nf_soft) == 1:
                    score = max(score, 80)
                elif strong_pair_overlap >= 1 and len(hint_soft) == 1 and len(nf_soft) == 1:
                    score = max(score, 80)
                elif strong_pair_overlap >= 1 and (len(hint_soft) == 1 or len(nf_soft) == 1):
                    # Avoid weak single-token collisions (e.g. "mam" matching rejected hint "Boro Mami").
                    score = max(score, 55)
        if score >= 70:
            matched.append((score, c))
    matched.sort(key=lambda x: -x[0])
    if not matched:
        return []
    best = matched[0][0]
    return [c for s, c in matched if s == best and s >= 80] or ([matched[0][1]] if best >= 70 else [])


def _extract_correction_followup(message: str, pending: dict[str, Any]) -> dict[str, Any] | None:
    """Detect correction/exclusion intent in a clarification follow-up.

    Returns trusted correction info (target query + rejected resource_names from pending candidates)
    or None when the message is not a correction.
    """
    text = (message or "").strip()
    if not text or not isinstance(pending, dict):
        return None
    candidates = [c for c in (pending.get("candidates") or []) if isinstance(c, dict)]
    if not candidates:
        return None

    lowered = text.casefold()
    # Selection-like responses are not corrections.
    if _SELECTION_ONLY_RE.match(text):
        return None

    has_reject = any(p.search(text) for p in _CORRECTION_REJECT_PATTERNS)
    has_not_prefix = bool(re.match(r"^\s*(?:no|nah|nope)\b", lowered))
    starts_with_not = bool(re.match(r"^\s*not\b", lowered))
    # "No, not ...", "Not Boro Mami..." are strong correction signals.
    strong_correction = has_reject or starts_with_not
    if not strong_correction and not (has_not_prefix and ("other" in lowered or "mean" in lowered)):
        return None

    # Extract rejected hint (name after "not"/"no, not") and desired target.
    rejected_hint = None
    m_rej = re.search(
        r"\b(?:not|no,?\s*not)\s+(?P<name>[A-Za-z0-9\u0980-\u09FF][A-Za-z0-9\u0980-\u09FF .'\-❤️]{0,80}?)(?=\s*(?:[,.;!?]|$|\b(?:i\s+want|i\s+meant|i\s+mean|show\s+me|give\s+me|please|can\s+you|could\s+you)\b))",
        text,
        re.IGNORECASE,
    )
    if m_rej:
        rejected_hint = re.sub(r"\s+", " ", m_rej.group("name")).strip().strip(" ,.!?\"'")
        rejected_hint = re.sub(r"\b(?:one|contact|person|number|phone|email|details)\b$", "", rejected_hint, flags=re.IGNORECASE).strip()

    target_query = None
    for pat in _CORRECTION_TARGET_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        candidate_target = re.sub(r"\s+", " ", (m.group("target") or "")).strip().strip(" ,.!?\"'")
        candidate_target = re.sub(r"\b(?:number|phone(?:\s+number)?|mobile|email(?:\s+address)?|contact|details|info|information|please)\b$", "", candidate_target, flags=re.IGNORECASE).strip().strip(" ,.!?\"'")
        candidate_target = re.sub(r"^(?:the|a|an)\s+", "", candidate_target, flags=re.IGNORECASE).strip()
        # Avoid using the rejected name as the intended target.
        if candidate_target and (not rejected_hint or _normalize_text(candidate_target) != _normalize_text(rejected_hint)):
            target_query = candidate_target
            break

    if not target_query and rejected_hint:
        # Try to infer target by removing rejection clause when a specific rejected name was mentioned.
        cleaned = re.sub(r"^\s*(?:no|nah|nope|not)\b[,:!\s-]*", "", text, flags=re.IGNORECASE)
        cleaned = re.sub(re.escape(rejected_hint), " ", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\b(?:not|no|nah|nope|i\s+want|i\s+meant|i\s+mean|show\s+me|give\s+me|the\s+other|another|one|contact|person)\b", " ", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s+", " ", cleaned).strip().strip(" ,.!?\"'")
        # Reuse target cleaner to extract a name-ish query from the remainder.
        target_query = _clean_target(cleaned)
        if target_query and _normalize_text(target_query) == _normalize_text(rejected_hint):
            target_query = None

    if not target_query:
        # Generic "other/someone else" without an identifiable intended target must not trigger a new search.
        if strong_correction or (has_not_prefix and ("other" in lowered or "mean" in lowered)):
            return {"kind": "correction_ambiguous", "target_query": None, "rejected_resource_names": []}
        return None

    # Guard against garbage targets.
    norm_target = _normalize_text(target_query)
    if not norm_target or norm_target in {"someone", "someone else", "other", "another", "else", "anyone", "anybody"}:
        return {"kind": "correction_ambiguous", "target_query": None, "rejected_resource_names": []}

    # Map rejected hint to trusted pending candidate resource_names only.
    rejected_resource_names: list[str] = []
    rejected_candidates = _match_candidate_by_name_hint(rejected_hint, candidates) if rejected_hint else []
    for c in rejected_candidates:
        rn = c.get("resource_name")
        if isinstance(rn, str) and _RESOURCE_NAME_RE.match(rn.strip()):
            rejected_resource_names.append(rn.strip())
    rejected_resource_names = list(dict.fromkeys(rejected_resource_names))

    # If message explicitly rejects but we cannot confidently identify which pending candidate,
    # still treat as correction with target search (do not select wrong one) and let search/results disambiguate.
    # But if target resolves to same as an explicitly named rejected candidate name, refuse unsafe guess.
    if rejected_hint and not rejected_resource_names and _normalize_text(target_query) == _normalize_text(rejected_hint):
        return {"kind": "correction_ambiguous", "target_query": None, "rejected_resource_names": []}

    return {
        "kind": "correction",
        "target_query": target_query,
        "rejected_resource_names": rejected_resource_names,
        "rejected_hint": rejected_hint,
    }


def _pending_to_outcome(pending: dict[str, Any]) -> dict[str, Any]:
    candidates = pending.get("candidates") or []
    summaries = [c for c in candidates if isinstance(c, dict)]
    return {
        "status": "multiple_matches",
        "kind": "search_contact",
        "requested_attribute": pending.get("requested_attribute") or "details",
        "target_query": pending.get("target_query"),
        "match_count": len(summaries),
        "candidate_summaries": summaries,
        "clarification_candidates": _clarification_candidates_for_prompt(summaries),
    }


async def _run_contacts_read(
    *,
    user_id: str,
    connection_id: str,
    message: str,
    conversation_id: str | None = None,
    pending_clarification: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> tuple[ContactsReadIntent, dict[str, Any], dict[str, Any] | None]:
    now = now or datetime.now(timezone.utc)
    tools = create_contacts_tools(user_id=user_id, connection_id=connection_id)
    by_name = {t.name: t for t in tools}

    # 1) Follow-up clarification: resolve only against stored trusted candidates.
    if conversation_id and _is_valid_pending_contacts_clarification(
        pending_clarification, user_id=user_id, conversation_id=conversation_id, now=now
    ):
        correction = _extract_correction_followup(message, pending_clarification)
        selected = None
        if correction is None:
            selected = _resolve_clarification_selection(message, pending_clarification.get("candidates") or [])

        if correction is None and selected is not None:
            rn = selected.get("resource_name")
            # Use requested attribute from follow-up if explicit; else keep pending attribute.
            followup_attr = _detect_requested_attribute(message)
            pending_attr = pending_clarification.get("requested_attribute") or "details"
            attr = followup_attr if followup_attr != "none" else (pending_attr if pending_attr in {"phone", "email", "details"} else "details")
            if attr == "phone":
                op = "get_contact_phone"
            elif attr == "email":
                op = "get_contact_email"
            else:
                op = "get_contact_details"
            intent = ContactsReadIntent(op, attr, pending_clarification.get("target_query"), source="clarification")
            get_tool = by_name.get("get_contact")
            if get_tool is None or not isinstance(rn, str):
                return intent, {"status": "error", "error": "Contacts read is unavailable."}, pending_clarification
            raw_get = await get_tool.ainvoke({"resource_name": rn})
            env_get = json.loads(raw_get)
            if not env_get.get("ok") or not isinstance(env_get.get("data"), dict):
                err = (env_get.get("error") or {}).get("message") or "Google Contacts request failed."
                # Recoverable/lookup error: preserve pending clarification so user can retry/select again.
                return intent, {"status": "error", "error": err}, pending_clarification
            contact = env_get["data"]
            returned_rn = contact.get("resource_name") or contact.get("resourceName")
            if returned_rn != rn:
                return intent, {"status": "error", "error": "The selected contact could not be verified."}, pending_clarification
            # Explicit user selection resolved against trusted candidates: return verified contact directly.
            minimal = _minimal_contact(contact)
            outcome: dict[str, Any] = {
                "status": "single_match",
                "kind": intent.intent,
                "requested_attribute": intent.requested_attribute,
                "target_query": intent.target_query,
                "matches": [minimal],
                "match_count": 1,
                "data_source": "google_people_api",
                "selected_contact_returned_by_api": True,
                "match_strategy": "clarification_selection_get_contact",
            }
            if attr == "phone":
                outcome["field_status"] = "present" if _contact_has_phone(contact) else "missing"
            elif attr == "email":
                outcome["field_status"] = "present" if _contact_has_email(contact) else "missing"
            else:
                outcome["field_status"] = "present"
            return intent, outcome, None

        if correction is not None:
            if correction.get("kind") == "correction_ambiguous" or not correction.get("target_query"):
                refreshed = dict(pending_clarification)
                refreshed["expires_at"] = (now + timedelta(seconds=CONTACTS_CLARIFICATION_TTL_SECONDS)).isoformat()
                if "clarification_id" not in refreshed or not refreshed.get("clarification_id"):
                    refreshed["clarification_id"] = str(uuid4())
                if "created_at" not in refreshed or not refreshed.get("created_at"):
                    refreshed["created_at"] = now.isoformat()
                intent = ContactsReadIntent(
                    "unknown",
                    pending_clarification.get("requested_attribute") or "details",
                    pending_clarification.get("target_query"),
                    ambiguous=True,
                    reason="correction_ambiguous",
                    source="clarification",
                )
                outcome = _pending_to_outcome(refreshed)
                outcome["message"] = (
                    "I understand you meant a different contact. Please tell me the exact contact name, "
                    "or reply with the number/name from the matching contacts."
                )
                return intent, outcome, refreshed

            followup_attr = _detect_requested_attribute(message)
            pending_attr = pending_clarification.get("requested_attribute") or "details"
            attr = followup_attr if followup_attr != "none" else (pending_attr if pending_attr in {"phone", "email", "details"} else "details")
            if attr == "phone":
                op = "get_contact_phone"
            elif attr == "email":
                op = "get_contact_email"
            else:
                op = "get_contact_details"
            target_query = str(correction["target_query"]).strip()
            intent = ContactsReadIntent(op, attr, target_query, source="clarification_correction")
            existing_rejected = pending_clarification.get("rejected_resource_names") if isinstance(pending_clarification.get("rejected_resource_names"), list) else []
            rejected_rns = list(dict.fromkeys([
                *(rn for rn in existing_rejected if isinstance(rn, str) and _RESOURCE_NAME_RE.match(rn.strip())),
                *(correction.get("rejected_resource_names") or []),
            ]))
            # Fall through to the normal search pipeline below using corrected target and exclusions.
            corrected_followup = {
                "intent": intent,
                "rejected_resource_names": set(rejected_rns),
                "base_pending": pending_clarification,
            }
        else:
            corrected_followup = None

        if correction is None and selected is None:
            # Not resolved. If user is asking for attribute on an unresolved referent, keep clarification and ask to choose.
            refreshed = dict(pending_clarification)
            refreshed["expires_at"] = (now + timedelta(seconds=CONTACTS_CLARIFICATION_TTL_SECONDS)).isoformat()
            if "clarification_id" not in refreshed or not refreshed.get("clarification_id"):
                refreshed["clarification_id"] = str(uuid4())
            if "created_at" not in refreshed or not refreshed.get("created_at"):
                refreshed["created_at"] = now.isoformat()
            intent = ContactsReadIntent(
                "unknown",
                pending_clarification.get("requested_attribute") or "details",
                pending_clarification.get("target_query"),
                ambiguous=True,
                reason="clarification_not_resolved",
                source="clarification",
            )
            outcome = _pending_to_outcome(refreshed)
            if _is_attribute_followup_without_selection(message, refreshed):
                outcome["message"] = (
                    "I need to know which contact you mean before I can share that information. "
                    "Please reply with the number or the exact contact name from the matching contacts."
                )
            return intent, outcome, refreshed
    else:
        corrected_followup = None

    # 2) Resolve intent (LLM structured extraction + deterministic validation/fallback) before any Google API call.
    if corrected_followup is not None:
        intent = corrected_followup["intent"]
        _extraction = None
        _source = "clarification_correction"
    else:
        intent, _extraction, _source = resolve_contacts_read_intent(message)

    # 3) Execute resolved intent
    if intent.intent == "count_contacts":
        tool = by_name.get("list_contacts")
        if tool is None:
            return intent, {"status": "error", "error": "Contacts read is unavailable."}, None
        raw = await tool.ainvoke({"page_size": 1000})
        env = json.loads(raw)
        if not env.get("ok"):
            err = env.get("error") or {}
            return intent, {"status": "error", "error": err.get("message") or "Google Contacts request failed."}, None
        data = env.get("data") or {}
        total = data.get("total_items")
        if total is None:
            total = data.get("total_people")
        if total is None:
            total = data.get("totalItems")
        if total is None:
            total = data.get("totalPeople")
        returned = len(data.get("connections") or data.get("contacts") or [])
        return intent, {
            "status": "count",
            "data_source": "google_people_api",
            "selected_contact_returned_by_api": False,
            "match_strategy": "count_contacts",
            "total": total,
            "returned_count": returned,
            "has_more": bool(data.get("next_page_token") or data.get("nextPageToken")),
        }, None

    if intent.intent == "list_contacts":
        tool = by_name.get("list_contacts")
        if tool is None:
            return intent, {"status": "error", "error": "Contacts read is unavailable."}, None
        raw = await tool.ainvoke({"page_size": 50})
        env = json.loads(raw)
        if not env.get("ok"):
            err = env.get("error") or {}
            return intent, {"status": "error", "error": err.get("message") or "Google Contacts request failed."}, None
        data = env.get("data") or {}
        contacts = data.get("contacts") or []
        minimal = [
            _minimal_contact(c)
            for c in contacts
            if isinstance(c, dict) and isinstance(c.get("resource_name"), str) and c.get("resource_name")
        ]
        total = data.get("total_items")
        if total is None:
            total = data.get("total_people")
        if total is None:
            total = data.get("totalItems")
        if total is None:
            total = data.get("totalPeople")
        return intent, {
            "status": "list",
            "data_source": "google_people_api",
            "selected_contact_returned_by_api": False,
            "match_strategy": "list_contacts",
            "contacts": minimal[:30],
            "returned_count": len(minimal),
            "total": total,
            "has_more": bool(data.get("next_page_token") or data.get("nextPageToken")),
        }, None

    if intent.intent in {"search_contact", "get_contact_phone", "get_contact_email", "get_contact_details"}:
        if not intent.target_query:
            return intent, {"status": "needs_clarification", "message": "Please tell me which contact you want to look up."}, None
        tool = by_name.get("search_contacts")
        if tool is None:
            return intent, {"status": "error", "error": "Contacts read is unavailable."}, None

        async def _search_query(search_query: str) -> tuple[list[dict[str, Any]], bool, str | None]:
            # Bounded pagination (read-only safety) for one query; returns actual API-backed results.
            q_collected: list[dict[str, Any]] = []
            q_seen_rns: set[str] = set()
            q_seen_page_tokens: set[str] = set()
            q_page_token = None
            q_pages = 0
            q_limited = False
            q_reason: str | None = None
            q_next = None
            while q_pages < CONTACTS_SEARCH_MAX_PAGES and len(q_collected) < CONTACTS_SEARCH_MAX_RESULTS:
                q_pages += 1
                args = {"query": search_query, "page_size": CONTACTS_SEARCH_PAGE_SIZE}
                if q_page_token:
                    if q_page_token in q_seen_page_tokens:
                        q_limited = True
                        q_reason = "repeated_page_token"
                        break
                    q_seen_page_tokens.add(q_page_token)
                    args["page_token"] = q_page_token
                raw = await tool.ainvoke(args)
                env = json.loads(raw)
                if not env.get("ok"):
                    err = env.get("error") or {}
                    message_err = err.get("message") or "Google Contacts request failed."
                    if q_collected:
                        q_limited = True
                        q_reason = "pagination_error"
                        break
                    raise RuntimeError(message_err)
                data = env.get("data") or {}
                contacts = data.get("contacts") or []
                if isinstance(contacts, list):
                    for c in contacts:
                        if not isinstance(c, dict):
                            continue
                        rn = c.get("resource_name")
                        if isinstance(rn, str) and rn:
                            if rn in q_seen_rns:
                                continue
                            q_seen_rns.add(rn)
                        if corrected_followup and isinstance(rn, str) and rn in corrected_followup.get("rejected_resource_names", set()):
                            continue
                        q_collected.append(c)
                        if len(q_collected) >= CONTACTS_SEARCH_MAX_RESULTS:
                            break
                q_next = data.get("next_page_token") or data.get("nextPageToken")
                if not q_next:
                    break
                if len(q_collected) >= CONTACTS_SEARCH_MAX_RESULTS:
                    q_limited = True
                    q_reason = "max_results"
                    break
                q_page_token = q_next
            else:
                if q_page_token or q_next:
                    q_limited = True
                    q_reason = q_reason or "max_pages"
            return q_collected, q_limited, q_reason

        try:
            collected, search_limited, limit_reason = await _search_query(intent.target_query)
        except RuntimeError as e:
            return intent, {"status": "error", "error": str(e)}, None

        outcome = _classify_search_results(intent=intent, contacts=collected)

        # Safe fallback: if initial query yielded no confident match, try conservative normalized variants
        # and classify only against actual Google People API results from those searches.
        # Fuzzy/variant matching is implemented locally by the application over returned API results;
        # Google People API search is not assumed to be fuzzy beyond provider behavior.
        if outcome.get("status") == "no_match" and not corrected_followup:
            variant_candidates: list[str] = []
            tq = (intent.target_query or "").strip()
            if tq:
                # Apostrophe-less possessive/context variant (e.g. "rintus" -> "rintu") only as fallback query.
                # Do not apply as unconditional extraction; genuine names ending with 's' remain intact as targets.
                if len(tq) > 3 and tq.casefold().endswith("s") and not tq.casefold().endswith("ss"):
                    v = tq[:-1].strip()
                    if v and v.casefold() != tq.casefold():
                        variant_candidates.append(v)
                # If target token appears in possessive-like request phrasing, try its base form.
                m_poss = re.search(
                    rf"\b({re.escape(tq)})\b(?=\s*(?:'s|’s)?\s*(?:number|phone|mobile|contact|email|details|info|address)\b)",
                    (message or ""),
                    re.IGNORECASE,
                )
                if m_poss and len(tq) > 3 and tq.casefold().endswith("s") and not tq.casefold().endswith("ss"):
                    v2 = tq[:-1].strip()
                    if v2 and v2.casefold() not in {q.casefold() for q in variant_candidates}:
                        variant_candidates.append(v2)
            # Dedupe preserve order, bounded attempts
            seen_q = {tq.casefold()}
            alt_queries = []
            for v in variant_candidates:
                key = v.casefold()
                if key and key not in seen_q:
                    seen_q.add(key)
                    alt_queries.append(v)
            merged = list(collected)
            merged_seen_rns = {
                c.get("resource_name") for c in collected
                if isinstance(c, dict) and isinstance(c.get("resource_name"), str)
            }
            any_limited = search_limited
            any_reason = limit_reason
            for alt_q in alt_queries[:2]:
                try:
                    alt_collected, alt_limited, alt_reason = await _search_query(alt_q)
                except RuntimeError:
                    continue
                any_limited = any_limited or alt_limited
                any_reason = any_reason or alt_reason
                for c in alt_collected:
                    rn = c.get("resource_name") if isinstance(c, dict) else None
                    if isinstance(rn, str):
                        if rn in merged_seen_rns:
                            continue
                        merged_seen_rns.add(rn)
                    merged.append(c)
                # Reclassify using original target so matching is conservative against returned contact names.
                outcome = _classify_search_results(intent=intent, contacts=merged)
                collected = merged
                search_limited = any_limited
                limit_reason = any_reason
                if outcome.get("status") != "no_match":
                    break

        # Grounded fallback for relationship/alias lookups (e.g. "hubby"/"spouse") when
        # searchContacts does not surface relation/nickname metadata matches. Bounded list scan.
        if outcome.get("status") == "no_match":
            target_query = (intent.target_query or "").strip()
            should_try_list_scan = bool(_relationship_alias_terms(target_query)) or len(target_query.split()) <= 3
            if should_try_list_scan:
                list_tool = by_name.get("list_contacts")
                if list_tool is not None:
                    list_collected: list[dict[str, Any]] = []
                    list_seen_rns = {
                        c.get("resource_name") for c in collected
                        if isinstance(c, dict) and isinstance(c.get("resource_name"), str)
                    }
                    list_limited = search_limited
                    list_reason = limit_reason
                    page_token = None
                    pages = 0
                    try:
                        while pages < 2 and len(list_collected) + len(collected) < 200:
                            pages += 1
                            args = {"page_size": 100}
                            if page_token:
                                args["page_token"] = page_token
                            raw_list = await list_tool.ainvoke(args)
                            env_list = json.loads(raw_list)
                            if not env_list.get("ok"):
                                break
                            data_list = env_list.get("data") or {}
                            contacts_list = data_list.get("contacts") or []
                            if isinstance(contacts_list, list):
                                for c in contacts_list:
                                    if not isinstance(c, dict):
                                        continue
                                    rn = c.get("resource_name")
                                    if corrected_followup and isinstance(rn, str) and rn in corrected_followup.get("rejected_resource_names", set()):
                                        continue
                                    if isinstance(rn, str):
                                        if rn in list_seen_rns:
                                            continue
                                        list_seen_rns.add(rn)
                                    list_collected.append(c)
                            page_token = data_list.get("next_page_token") or data_list.get("nextPageToken")
                            if not page_token:
                                break
                            list_limited = True
                            list_reason = list_reason or "list_scan_bounded"
                        if list_collected:
                            merged_scan = list(collected) + list_collected
                            scan_outcome = _classify_search_results(intent=intent, contacts=merged_scan)
                            if scan_outcome.get("status") != "no_match":
                                outcome = scan_outcome
                                collected = merged_scan
                                search_limited = list_limited
                                limit_reason = list_reason
                            elif list_limited:
                                collected = merged_scan
                                search_limited = True
                                limit_reason = list_reason or "list_scan_bounded"
                    except Exception:
                        # Safe fallback: keep original search-based outcome.
                        pass

        if outcome.get("status") == "no_match" and search_limited:
            outcome = {
                "status": "incomplete_search",
                "kind": intent.intent,
                "requested_attribute": intent.requested_attribute,
                "target_query": intent.target_query,
                "match_count": 0,
                "matches": [],
                "scanned_count": len(collected),
                "max_pages": CONTACTS_SEARCH_MAX_PAGES,
                "max_results": CONTACTS_SEARCH_MAX_RESULTS,
                "limit_reason": limit_reason or "search_limit",
                "message": (
                    "I searched part of your Google Contacts but couldn’t confidently find a matching contact "
                    "within the search limit. Please try a more specific name, email, or phone number."
                ),
            }
        elif outcome.get("status") == "multiple_matches" and search_limited:
            outcome["incomplete_search"] = True
            outcome["search_limited"] = True
            outcome["limit_reason"] = limit_reason or "search_limit"
            outcome["scanned_count"] = len(collected)
            outcome["max_pages"] = CONTACTS_SEARCH_MAX_PAGES
            outcome["max_results"] = CONTACTS_SEARCH_MAX_RESULTS

        if outcome.get("status") == "single_match" and intent.intent in {"get_contact_details", "get_contact_phone", "get_contact_email", "search_contact"}:
            rn = (outcome.get("matches") or [{}])[0].get("resource_name")
            get_tool = by_name.get("get_contact")
            if not isinstance(rn, str) or not rn or get_tool is None:
                outcome = {"status": "error", "error": "I couldn't retrieve this contact's details right now. Please try again."}
            else:
                try:
                    raw_get = await get_tool.ainvoke({"resource_name": rn})
                    env_get = json.loads(raw_get)
                    if not env_get.get("ok") or not isinstance(env_get.get("data"), dict):
                        err = (env_get.get("error") or {}).get("message") if isinstance(env_get.get("error"), dict) else None
                        outcome = {"status": "error", "error": "I couldn't retrieve this contact's details right now. Please try again."}
                    else:
                        detailed = env_get["data"]
                        drn = detailed.get("resource_name") or detailed.get("resourceName")
                        if drn != rn:
                            outcome = {"status": "error", "error": "I couldn't verify this contact from Google Contacts. Please try again."}
                        else:
                            authoritative = _minimal_contact(detailed)
                            outcome["matches"] = [authoritative]
                            outcome["data_source"] = "google_people_api"
                            outcome["selected_contact_returned_by_api"] = True
                            # Recompute field availability from the authoritative
                            # get_contact payload so provenance/observability cannot
                            # claim a value was validated when it is missing.
                            if intent.requested_attribute == "phone":
                                outcome["field_status"] = (
                                    "present"
                                    if _contact_has_phone(authoritative)
                                    else "missing"
                                )
                            elif intent.requested_attribute == "email":
                                outcome["field_status"] = (
                                    "present"
                                    if _contact_has_email(authoritative)
                                    else "missing"
                                )
                            else:
                                outcome["field_status"] = "present"
                except Exception:
                    outcome = {"status": "error", "error": "I couldn't retrieve this contact's details right now. Please try again."}

        # If the user explicitly asked for all matching contacts' requested values (e.g. "all phone numbers
        # associated with Subhro"), fetch authoritative details for each via get_contact and render fail-soft.
        if (
            outcome.get("status") == "multiple_matches"
            and intent.requested_attribute in {"phone", "email", "details"}
            and _requests_all_matching_contacts(message, intent.requested_attribute)
            and isinstance(outcome.get("matches"), list)
            and len(outcome.get("matches") or []) >= 1
        ):
            get_tool = by_name.get("get_contact")
            authoritative_matches: list[dict[str, Any]] = []
            fetch_failures: list[dict[str, Any]] = []
            for m in outcome.get("matches") or []:
                if not isinstance(m, dict):
                    continue
                rn = m.get("resource_name")
                if not isinstance(rn, str) or not rn or get_tool is None:
                    fetch_failures.append({"resource_name": rn, "display_name": _display_name(m) if isinstance(m, dict) else "Unknown contact"})
                    continue
                try:
                    raw_get = await get_tool.ainvoke({"resource_name": rn})
                    env_get = json.loads(raw_get)
                    if not env_get.get("ok") or not isinstance(env_get.get("data"), dict):
                        fetch_failures.append({"resource_name": rn, "display_name": _display_name(m)})
                        continue
                    detailed = env_get["data"]
                    drn = detailed.get("resource_name") or detailed.get("resourceName")
                    if drn != rn:
                        fetch_failures.append({"resource_name": rn, "display_name": _display_name(m)})
                        continue
                    authoritative_matches.append(_minimal_contact(detailed))
                except Exception:
                    fetch_failures.append({"resource_name": rn, "display_name": _display_name(m)})
            if not authoritative_matches and fetch_failures:
                outcome = {"status": "error", "error": "I couldn't retrieve those contacts' details right now. Please try again."}
            else:
                outcome["status"] = "multiple_matches_all"
                outcome["match_strategy"] = (outcome.get("match_strategy") or "multiple_matches") + "_all_requested"
                outcome["selected_contact_returned_by_api"] = True
                outcome["data_source"] = "google_people_api"
                outcome["matches"] = authoritative_matches
                if fetch_failures:
                    outcome["partial_failures"] = [{"display_name": f.get("display_name", "Unknown contact")} for f in fetch_failures]

        pending = None
        if outcome.get("status") == "multiple_matches" and conversation_id:
            pending = {
                "version": 1,
                "type": "contacts_read_clarification",
                "clarification_id": str(uuid4()),
                "created_at": now.isoformat(),
                "user_id": str(user_id),
                "conversation_id": str(conversation_id),
                "expires_at": (now + timedelta(seconds=CONTACTS_CLARIFICATION_TTL_SECONDS)).isoformat(),
                "target_query": intent.target_query,
                "requested_attribute": intent.requested_attribute,
                "candidates": outcome.get("candidate_summaries") or [],
            }
            if corrected_followup and corrected_followup.get("rejected_resource_names"):
                pending["rejected_resource_names"] = sorted(
                    [rn for rn in corrected_followup["rejected_resource_names"] if isinstance(rn, str) and _RESOURCE_NAME_RE.match(rn)]
                )
            outcome["clarification_candidates"] = _clarification_candidates_for_prompt(outcome.get("candidate_summaries") or [])
        elif corrected_followup and conversation_id and outcome.get("status") in {"no_match", "incomplete_search"}:
            # Keep exclusion context for a short follow-up if the corrected search didn't resolve safely.
            base_pending = corrected_followup.get("base_pending") or {}
            pending = {
                "version": 1,
                "type": "contacts_read_clarification",
                "clarification_id": base_pending.get("clarification_id") or str(uuid4()),
                "created_at": base_pending.get("created_at") or now.isoformat(),
                "user_id": str(user_id),
                "conversation_id": str(conversation_id),
                "expires_at": (now + timedelta(seconds=CONTACTS_CLARIFICATION_TTL_SECONDS)).isoformat(),
                "target_query": intent.target_query,
                "requested_attribute": intent.requested_attribute,
                "candidates": base_pending.get("candidates") or [],
                "rejected_resource_names": sorted([
                    rn for rn in (corrected_followup.get("rejected_resource_names") or set())
                    if isinstance(rn, str) and _RESOURCE_NAME_RE.match(rn)
                ]),
            }
            if not _is_valid_pending_contacts_clarification(pending, user_id=str(user_id), conversation_id=str(conversation_id), now=now):
                pending = None
        return intent, outcome, pending

    reason = intent.reason or ""
    msg = "Please tell me which contact you want me to look up, or ask to list your contacts."
    if reason == "missing_target":
        msg = "Please tell me the contact's name so I can look it up."
    return intent, {"status": "needs_clarification", "message": msg, "reason": reason}, None


def generate_contacts_read_response(
    *,
    user_id: str,
    connection_id: str,
    message: str,
    conversation_id: str | None = None,
    pending_clarification: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run Contacts read and produce a backend-grounded response.

    Contact data (names/phones/emails/details) shown to the user is rendered only from
    verified backend/Google Contacts results. The LLM is never used as a source of contact info.
    """
    import asyncio

    now = datetime.now(timezone.utc)
    try:
        intent, outcome, new_pending = asyncio.run(
            _run_contacts_read(
                user_id=user_id,
                connection_id=connection_id,
                message=message,
                conversation_id=conversation_id,
                pending_clarification=pending_clarification,
                now=now,
            )
        )
    except Exception:
        # Do not drop a valid pending clarification on transient/recoverable execution errors.
        keep_pending = pending_clarification if (
            conversation_id and _is_valid_pending_contacts_clarification(
                pending_clarification, user_id=str(user_id), conversation_id=str(conversation_id), now=now
            )
        ) else None
        response = "I couldn't retrieve your Google Contacts right now. Please try again."
        return {
            "assistant_response": response,
            "provider": "",
            "model": "",
            "messages": [
                {"role": "user", "content": message},
                {"role": "assistant", "content": response},
            ],
            "pending_contacts_clarification": keep_pending,
        }

    if outcome.get("status") == "error":
        err = str(outcome.get("error") or "Google Contacts request failed.")
        safe = "I couldn't retrieve your Google Contacts right now. Please try again."
        lowered = err.casefold()
        if "permission" in lowered or "scope" in lowered or "not connected" in lowered or "connected apps" in lowered:
            safe = err
        provenance = {
            "request_id": (_request_id() or None),
            "authenticated_user_ref": _safe_ref(str(user_id)),
            "google_connection_ref": _safe_ref(str(connection_id)),
            "conversation_ref": _safe_ref(str(conversation_id)) if conversation_id else None,
            "api_invocation_status": "invoked" if outcome.get("data_source") == "google_people_api" else "unknown",
            "outcome_status": "error",
            "verified_source": False,
        }
        try:
            logger.info(
                "contacts_read_provenance | request_id=%s | user_ref=%s | connection_ref=%s | conversation_ref=%s | status=error | api=%s | verified=false",
                provenance.get("request_id"), provenance.get("authenticated_user_ref"), provenance.get("google_connection_ref"), provenance.get("conversation_ref"), provenance.get("api_invocation_status"),
            )
        except Exception:
            pass
        return {
            "assistant_response": safe,
            "provider": "",
            "model": "",
            "messages": [
                {"role": "user", "content": message},
                {"role": "assistant", "content": safe},
            ],
            "pending_contacts_clarification": new_pending,
            "contacts_provenance": provenance,
        }

    status = outcome.get("status") if isinstance(outcome, dict) else None
    if status == "single_match" and not (
        isinstance(outcome, dict)
        and outcome.get("data_source") == "google_people_api"
        and outcome.get("selected_contact_returned_by_api") is True
        and isinstance((outcome.get("matches") or [None])[0], dict)
    ):
        # Safety: never display contact details without traceable verified Google People API provenance for this request.
        safe = "I couldn't verify the contact details from Google Contacts for this request. Please try again."
        provenance = {
            "request_id": (_request_id() or None),
            "authenticated_user_ref": _safe_ref(str(user_id)),
            "google_connection_ref": _safe_ref(str(connection_id)),
            "conversation_ref": _safe_ref(str(conversation_id)) if conversation_id else None,
            "api_invocation_status": "unknown",
            "outcome_status": status,
            "verified_source": False,
            "provenance_validation": "failed_unverified_single_match",
        }
        try:
            logger.info("contacts_read_provenance_blocked_unverified | request_id=%s | user_ref=%s | conversation_ref=%s", provenance.get("request_id"), provenance.get("authenticated_user_ref"), provenance.get("conversation_ref"))
        except Exception:
            pass
        return {
            "assistant_response": safe,
            "provider": "",
            "model": "",
            "messages": [
                {"role": "user", "content": message},
                {"role": "assistant", "content": safe},
            ],
            "pending_contacts_clarification": None,
            "contacts_provenance": provenance,
        }
    response_text = _render_trusted_response(outcome)
    # Safe provenance metadata (no phone/email/token values). API invocation inferred from grounded read statuses.
    api_invoked_statuses = {
        "single_match",
        "multiple_matches",
        "multiple_matches_all",
        "no_match",
        "list",
        "count",
        "incomplete_search",
    }
    api_invoked = status in api_invoked_statuses
    selected = (outcome.get("matches") or [None])[0] if isinstance(outcome, dict) and isinstance(outcome.get("matches"), list) else None
    selected_ref = _safe_ref(selected.get("resource_name")) if isinstance(selected, dict) else None
    provenance = {
        "request_id": (_request_id() or None),
        "authenticated_user_ref": _safe_ref(str(user_id)),
        "google_connection_ref": _safe_ref(str(connection_id)),
        "conversation_ref": _safe_ref(str(conversation_id)) if conversation_id else None,
        "api_invocation_status": "invoked" if api_invoked else "not_invoked",
        "api_result_count": int(outcome.get("match_count") or outcome.get("returned_count") or 0) if isinstance(outcome, dict) else 0,
        "selected_contact_resource_ref": selected_ref,
        "match_strategy": outcome.get("match_strategy") if isinstance(outcome, dict) else None,
        "outcome_status": status,
        "displayed_phone_from_validated_api": bool(
            status in {"single_match", "multiple_matches_all"}
            and outcome.get("requested_attribute") == "phone"
            and (outcome.get("field_status") == "present" or status == "multiple_matches_all")
            and outcome.get("data_source") == "google_people_api"
            and outcome.get("selected_contact_returned_by_api")
        ),
        "displayed_email_from_validated_api": bool(
            status in {"single_match", "multiple_matches_all"}
            and outcome.get("requested_attribute") == "email"
            and (outcome.get("field_status") == "present" or status == "multiple_matches_all")
            and outcome.get("data_source") == "google_people_api"
            and outcome.get("selected_contact_returned_by_api")
        ),
        "verified_source": bool(outcome.get("data_source") == "google_people_api") if isinstance(outcome, dict) else False,
    }
    try:
        logger.info(
            "contacts_read_provenance | request_id=%s | user_ref=%s | connection_ref=%s | conversation_ref=%s | status=%s | match_strategy=%s | api=%s | result_count=%s | selected_ref=%s | phone_from_api=%s | email_from_api=%s | verified=%s",
            provenance.get("request_id"),
            provenance.get("authenticated_user_ref"),
            provenance.get("google_connection_ref"),
            provenance.get("conversation_ref"),
            provenance.get("outcome_status"),
            provenance.get("match_strategy"),
            provenance.get("api_invocation_status"),
            provenance.get("api_result_count"),
            provenance.get("selected_contact_resource_ref"),
            provenance.get("displayed_phone_from_validated_api"),
            provenance.get("displayed_email_from_validated_api"),
            provenance.get("verified_source"),
        )
    except Exception:
        pass
    result: dict[str, Any] = {
        "assistant_response": response_text,
        "provider": "",
        "model": "",
        "messages": [
            {"role": "user", "content": message},
            {"role": "assistant", "content": response_text},
        ],
        "pending_contacts_clarification": new_pending,
        "contacts_provenance": provenance,
    }
    return result


# Compatibility helper retained for existing callers/tests that import it.
# It now returns a deterministic, backend-grounded response (no LLM-generated contact data).
def _safe_llm_fallback(outcome: dict[str, Any]) -> str:
    return _render_trusted_response(outcome)
