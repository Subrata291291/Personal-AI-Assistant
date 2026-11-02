
import logging

from app.llm.fallback import FallbackManager
from app.llm.schemas import LLMRequest, LLMResponse


logger = logging.getLogger(__name__)


class LLMRouter:

    def __init__(self, fallback_manager: FallbackManager | None = None):
        self.fallback_manager = (
            fallback_manager or FallbackManager()
        )

    def generate(self, request: LLMRequest) -> LLMResponse:

        logger.info(
            "LLM request received | messages=%s",
            len(request.messages),
        )

        response = self.fallback_manager.generate(request)

        logger.info(
            "LLM request completed | provider=%s | model=%s",
            response.provider,
            response.model,
        )

        return response


# Shared router instance for application components
llm_router = LLMRouter()
