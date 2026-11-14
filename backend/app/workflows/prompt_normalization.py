import re


_LEADING_LIST_MARKER = re.compile(
    r"^\s*(?:\d+[.):]|[-*•‣])\s+",
)
# Common misspellings of "calendar" should not change which integration a
# request is routed to. This only normalizes the routing copy of the prompt;
# the stored user message is never rewritten.
_CALENDAR_MISSPELLING = re.compile(r"\bcal[ae]nd[ae]r\b", re.IGNORECASE)


def _preserve_calendar_case(match: re.Match[str]) -> str:
    spelling = "calendar"
    return spelling.capitalize() if match.group(0)[0].isupper() else spelling


def normalize_prompt_for_routing(text: str) -> str:
    normalized = _LEADING_LIST_MARKER.sub("", text.strip(), count=1)
    return _CALENDAR_MISSPELLING.sub(_preserve_calendar_case, normalized)