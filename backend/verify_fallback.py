
from app.llm.fallback import FallbackManager
from app.llm.schemas import LLMRequest, LLMResponse


class MockGroqProvider:

    provider_name = "groq"

    def generate(self, request):
        print("Groq: Simulating timeout...")
        raise TimeoutError("Simulated Groq timeout")


class MockOpenRouterProvider:

    provider_name = "openrouter"

    def generate(self, request):
        print("OpenRouter: Simulating successful response...")

        return LLMResponse(
            content="Fallback successful!",
            provider="openrouter",
            model="mock-openrouter-model",
            latency_ms=120.0,
        )


class MockRegistry:

    def __init__(self):
        self.providers = {
            "groq": MockGroqProvider(),
            "openrouter": MockOpenRouterProvider(),
        }

        self.calls = []

    def get_provider(self, provider_name):
        self.calls.append(provider_name)
        return self.providers[provider_name]


registry = MockRegistry()

fallback_manager = FallbackManager(registry=registry)

fallback_manager.provider_priority = [
    "groq",
    "openrouter",
]

request = LLMRequest(
    messages=[
        {
            "role": "user",
            "content": "Test fallback behavior.",
        }
    ]
)

response = fallback_manager.generate(request)

print("\n========== FALLBACK RESULT ==========")
print("Final Provider:", response.provider)
print("Response:", response.content)
print("Provider Calls:", registry.calls)

if registry.calls == ["groq", "openrouter"]:
    print("\nFALLBACK SIMULATION: SUCCESS")
else:
    print("\nFALLBACK SIMULATION: FAILED")
