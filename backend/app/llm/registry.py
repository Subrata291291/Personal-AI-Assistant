
from app.llm.providers.base import BaseLLMProvider
from app.llm.providers.groq import GroqProvider
from app.llm.providers.openrouter import OpenRouterProvider
from app.llm.providers.gemini import GeminiProvider
from app.llm.providers.openai import OpenAIProvider


class ProviderRegistry:

    def __init__(self):
        self._provider_classes = {
            "groq": GroqProvider,
            "openrouter": OpenRouterProvider,
            "gemini": GeminiProvider,
            "openai": OpenAIProvider,
        }

        self._instances: dict[str, BaseLLMProvider] = {}

    def get_provider(self, provider_name: str) -> BaseLLMProvider:

        provider_name = provider_name.strip().lower()

        if provider_name not in self._provider_classes:
            raise ValueError(
                f"Unsupported LLM provider: {provider_name}"
            )

        if provider_name not in self._instances:
            provider_class = self._provider_classes[provider_name]
            self._instances[provider_name] = provider_class()

        return self._instances[provider_name]

    def get_registered_providers(self) -> list[str]:
        return list(self._provider_classes.keys())
    
    def is_configured(self, provider_name: str) -> bool:
        from app.core.config import settings

        configuration = {
            "groq": (settings.GROQ_API_KEY, settings.GROQ_MODEL),
            "openrouter": (
                settings.OPENROUTER_API_KEY,
                settings.OPENROUTER_MODEL,
            ),
            "gemini": (settings.GEMINI_API_KEY, settings.GEMINI_MODEL),
            "openai": (settings.OPENAI_API_KEY, settings.OPENAI_MODEL),
        }

        if provider_name not in configuration:
            return False

        api_key, model = configuration[provider_name]

        return bool(api_key and model and model.strip())
