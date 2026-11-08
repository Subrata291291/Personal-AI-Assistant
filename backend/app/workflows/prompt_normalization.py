import re


_LEADING_LIST_MARKER = re.compile(
    r"^\s*(?:\d+[.):]|[-*•‣])\s+",
)


def normalize_prompt_for_routing(text: str) -> str:
    return _LEADING_LIST_MARKER.sub("", text.strip(), count=1)