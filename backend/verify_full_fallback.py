
from app.llm.fallback import FallbackManager
from app.llm.schemas import LLMRequest, LLMResponse


class MockProvider:

    def __init__(self, name, should_fail):
        self.provider_name = name
        self.should_fail = should_fail

    def generate(self, request):
        print(f"{self.provider_name}: Called")

        if self.should_fail:
            raise TimeoutError(
                f"Simulated {self.provider_name} timeout"
            )

        return LLMResponse(
            content=f"Response from {self.provider_name}",
            provider=self.provider_name,
            model=f"mock-{self.provider_name}",
            latency_ms=100.0,
        )


class MockRegistry:

    def __init__(self):
        self.providers = {
            "groq": MockProvider("groq", True),
            "openrouter": MockProvider("openrouter", True),
            "gemini": MockProvider("gemini", True),
            "openai": MockProvider("openai", False),
        }

        self.calls = []

    def get_provider(self, name):
        self.calls.append(name)
        return self.providers[name]


registry = MockRegistry()

manager = FallbackManager(registry=registry)

manager.provider_priority = [
    "groq",
    "openrouter",
    "gemini",
    "openai",
]

request = LLMRequest(
    messages=[
        {
            "role": "user",
            "content": "Test complete fallback chain.",
        }
    ]
)

response = manager.generate(request)

print("\n========== COMPLETE FALLBACK ==========")
print("Final Provider:", response.provider)
print("Response:", response.content)
print("Provider Calls:", registry.calls)

expected = ["groq", "openrouter", "gemini", "openai"]

if registry.calls == expected and response.provider == "openai":
    print("\nCOMPLETE FALLBACK: SUCCESS")
else:
    print("\nCOMPLETE FALLBACK: FAILED")
