
from types import SimpleNamespace

from app.llm.fallback import FallbackManager
from app.llm.schemas import LLMRequest


class MockGroqProvider:

    provider_name = "groq"

    def generate(self, request):
        print("Groq: Simulating 401 Unauthorized")

        raise SimpleNamespaceError(
            "Invalid API credentials",
            status_code=401,
        )


class SimpleNamespaceError(Exception):

    def __init__(self, message, status_code):
        super().__init__(message)
        self.status_code = status_code


class MockRegistry:

    def __init__(self):
        self.calls = []

    def get_provider(self, name):
        self.calls.append(name)
        return MockGroqProvider()


registry = MockRegistry()

manager = FallbackManager(registry=registry)
manager.provider_priority = ["groq", "openrouter", "gemini", "openai"]

request = LLMRequest(
    messages=[
        {
            "role": "user",
            "content": "Test non-retryable error.",
        }
    ]
)

try:
    manager.generate(request)

except SimpleNamespaceError as error:
    print("\nError:", error)
    print("Status Code:", error.status_code)
    print("Provider Calls:", registry.calls)

    if registry.calls == ["groq"]:
        print("\nNON-RETRYABLE ERROR: SUCCESS")
    else:
        print("\nNON-RETRYABLE ERROR: FAILED")

