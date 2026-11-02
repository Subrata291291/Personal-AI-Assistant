
from abc import ABC, abstractmethod

from app.llm.schemas import LLMRequest, LLMResponse


class BaseLLMProvider(ABC):
    """
    Common interface for all LLM providers.

    Every provider must implement the generate() method.
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
