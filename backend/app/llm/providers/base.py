
from abc import ABC, abstractmethod
from collections.abc import Iterator

from app.llm.schemas import LLMRequest, LLMResponse, LLMStreamChunk


class BaseLLMProvider(ABC):
    """
    Common interface for all LLM providers.

    Every provider must implement generate(). Providers that support
    genuine token streaming may override stream(); the default raises
    NotImplementedError so callers can fall back appropriately.
    """

    provider_name: str

    @abstractmethod
    def generate(
        self,
        request: LLMRequest,
    ) -> LLMResponse:
        """
        Generate an LLM response.

        Each provider implements its own API integration
        but returns the same LLMResponse structure.
        """
        raise NotImplementedError

    def stream(
        self,
        request: LLMRequest,
    ) -> Iterator[LLMStreamChunk]:
        """Yield incremental response chunks when supported."""
        raise NotImplementedError("Streaming is not implemented for this provider.")
