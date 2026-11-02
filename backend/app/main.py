
from fastapi import FastAPI

from app.api.v1.endpoints.health import router as health_router
from app.core.config import settings
from app.core.logging import setup_logging, get_logger
from app.api.v1.endpoints.users import router as users_router
from app.api.v1.endpoints.conversations import router as conversations_router
from app.api.v1.endpoints.messages import router as messages_router
from app.api.v1.endpoints.chat import router as chat_router


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