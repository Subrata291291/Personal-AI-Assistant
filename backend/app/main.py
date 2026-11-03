
from fastapi import FastAPI

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
    allow_origins=[
        "http://localhost:5173",
        settings.FRONTEND_URL,
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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