
import time
from uuid import UUID, uuid4

from fastapi import FastAPI, Request

from app.api.v1.endpoints.health import router as health_router
from app.core.config import settings
from app.core.logging import setup_logging, get_logger
from app.api.v1.endpoints.users import router as users_router
from app.api.v1.endpoints.conversations import router as conversations_router
from app.api.v1.endpoints.messages import router as messages_router
from app.api.v1.endpoints.chat import router as chat_router
from app.api.v1.endpoints.oauth import router as oauth_router
from fastapi.middleware.cors import CORSMiddleware
from app.api.v1.endpoints.memory import router as memory_router
from app.api.v1.endpoints.memory_reviews import router as memory_reviews_router
from app.api.v1.endpoints.gmail import router as gmail_router
from app.api.v1.endpoints.calendar import router as calendar_router
from app.api.v1.endpoints.action_approvals import (
    router as action_approvals_router,
)
from app.core.request_metrics import RequestMetrics, current_request_metrics

setup_logging()

logger = get_logger(__name__)
logger.info("Personal AI Assistant application starting")

app = FastAPI(
    title=settings.APP_NAME,
    description=(
        "A multi-user AI assistant platform "
        "with memory, tools, and intelligent workflows."
    ),
    version="1.0.0",
    debug=settings.DEBUG,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_metrics_middleware(request: Request, call_next):
    incoming_request_id = request.headers.get("X-Request-ID")
    try:
        correlation_id = str(UUID(incoming_request_id))
    except (ValueError, TypeError, AttributeError):
        correlation_id = str(uuid4())

    metrics = RequestMetrics(request_id=correlation_id)
    context_token = current_request_metrics.set(metrics)
    started_at = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        response.headers["X-Request-ID"] = correlation_id
        return response
    finally:
        duration_ms = (time.perf_counter() - started_at) * 1000
        route = request.scope.get("route")
        route_template = getattr(route, "path", "unmatched")
        logger.info(
            "http_request_completed | request_id=%s | method=%s "
            "| route=%s | status=%s | duration_ms=%.2f | "
            "auth_ms=%.2f | db_queries=%s | db_ms=%.2f | "
            "google_calendar_api_calls=%s | google_calendar_api_ms=%.2f | "
            "gmail_api_calls=%s | gmail_api_ms=%.2f | "
            "google_oauth_calls=%s | google_oauth_ms=%.2f | "
            "connection_lock_acquisitions=%s | connection_lock_ms=%.2f | "
            "credential_lookups=%s | credential_lookup_ms=%.2f | "
            "retry_backoff_ms=%.2f | "
            "llm_calls=%s | llm_ms=%.2f | llm_provider_calls=%s "
            "| llm_provider_ms=%.2f | embedding_calls=%s | "
            "embedding_ms=%.2f | graph_nodes_ms=%s",
            correlation_id,
            request.method,
            route_template,
            status_code,
            duration_ms,
            metrics.auth_duration_ms,
            metrics.db_query_count,
            metrics.db_duration_ms,
            metrics.google_calendar_api_call_count,
            metrics.google_calendar_api_duration_ms,
            metrics.gmail_api_call_count,
            metrics.gmail_api_duration_ms,
            metrics.google_oauth_call_count,
            metrics.google_oauth_duration_ms,
            metrics.connection_lock_count,
            metrics.connection_lock_duration_ms,
            metrics.credential_lookup_count,
            metrics.credential_lookup_duration_ms,
            metrics.retry_backoff_duration_ms,
            metrics.llm_call_count,
            metrics.llm_duration_ms,
            metrics.llm_provider_call_count,
            metrics.llm_provider_duration_ms,
            metrics.embedding_call_count,
            metrics.embedding_duration_ms,
            {
                name: round(value, 2)
                for name, value in metrics.node_durations_ms.items()
            },
        )
        current_request_metrics.reset(context_token)


app.include_router(
    health_router,
    prefix=settings.API_V1_PREFIX,
    tags=["Health"],
)

app.include_router(
    users_router,
    prefix=settings.API_V1_PREFIX,
    tags=["Users"],
)

app.include_router(
    conversations_router,
    prefix=f"{settings.API_V1_PREFIX}/conversations",
    tags=["Conversations"],
)

app.include_router(
    messages_router,
    prefix=f"{settings.API_V1_PREFIX}/conversations/{{conversation_id}}/messages",
    tags=["Messages"],
)

app.include_router(
    chat_router,
    prefix=f"{settings.API_V1_PREFIX}/chat",
    tags=["Chat"],
)

app.include_router(
    oauth_router,
    prefix=f"{settings.API_V1_PREFIX}/oauth",
    tags=["OAuth"],
)

app.include_router(
    memory_router,
    prefix=f"{settings.API_V1_PREFIX}/memories",
    tags=["Memories"]
)

app.include_router(
    memory_reviews_router,
    prefix=f"{settings.API_V1_PREFIX}/memory-reviews",
    tags=["Memory Reviews"],
)

app.include_router(
    gmail_router,
    prefix=f"{settings.API_V1_PREFIX}/gmail",
    tags=["Gmail"],
)

app.include_router(
    calendar_router,
    prefix=f"{settings.API_V1_PREFIX}/calendar",
    tags=["Google Calendar"],
)

app.include_router(
    action_approvals_router,
    prefix=f"{settings.API_V1_PREFIX}/action-approvals",
    tags=["Action approvals"],
)