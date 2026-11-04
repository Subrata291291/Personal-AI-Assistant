from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass
class RequestMetrics:
    request_id: str
    auth_duration_ms: float = 0
    db_duration_ms: float = 0
    db_query_count: int = 0
    gmail_api_duration_ms: float = 0
    gmail_api_call_count: int = 0
    llm_duration_ms: float = 0
    llm_call_count: int = 0
    llm_provider_duration_ms: float = 0
    llm_provider_call_count: int = 0
    embedding_duration_ms: float = 0
    embedding_call_count: int = 0
    node_durations_ms: dict[str, float] = field(default_factory=dict)


current_request_metrics: ContextVar[RequestMetrics | None] = ContextVar(
    "current_request_metrics",
    default=None,
)


def record_timing(stage: str, duration_ms: float) -> None:
    metrics = current_request_metrics.get()
    if metrics is None:
        return

    if stage == "auth":
        metrics.auth_duration_ms += duration_ms
    elif stage == "database":
        metrics.db_duration_ms += duration_ms
        metrics.db_query_count += 1
    elif stage == "gmail_api":
        metrics.gmail_api_duration_ms += duration_ms
        metrics.gmail_api_call_count += 1
    elif stage == "llm":
        metrics.llm_duration_ms += duration_ms
        metrics.llm_call_count += 1
    elif stage == "llm_provider":
        metrics.llm_provider_duration_ms += duration_ms
        metrics.llm_provider_call_count += 1
    elif stage == "embedding":
        metrics.embedding_duration_ms += duration_ms
        metrics.embedding_call_count += 1
    else:
        metrics.node_durations_ms[stage] = (
            metrics.node_durations_ms.get(stage, 0) + duration_ms
        )


def request_id() -> str:
    metrics = current_request_metrics.get()
    return metrics.request_id if metrics else "-"
