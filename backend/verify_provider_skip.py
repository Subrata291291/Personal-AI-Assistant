
from app.llm.fallback import FallbackManager
from app.llm.schemas import LLMRequest, LLMResponse


class MockProvider:

    def __init__(self, name, should_fail):
        self.provider_name = name
        self.should_fail = should_fail

    def generate(self, request):
        print(f"{self.provider_name}: Called")

        if self.should_fail:
            raise TimeoutError(f"{self.provider_name} timeout")

        return LLMResponse(
            content=f"Response from {self.provider_name}",
            provider=self.provider_name,
            model=f"mock-{self.provider_name}",
            latency_ms=100.0,
        )


class MockRegistry:

    def __init__(self):
        self.configured = {
            "groq": True,
            "openrouter": False,
            "gemini": True,
            "openai": True,
        }

        self.providers = {
            "groq": MockProvider("groq", True),
            "gemini": MockProvider("gemini", True),
            "openai": MockProvider("openai", False),
        }

        self.calls = []

    def is_configured(self, name):
        return self.configured.get(name, False)

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
            "content": "Verify provider skipping.",
        }
    ]
)

response = manager.generate(request)

print("\n========== PROVIDER SKIP RESULT ==========")
print("Final Provider:", response.provider)
print("Provider Calls:", registry.calls)

expected = ["groq", "gemini", "openai"]

if registry.calls == expected and response.provider == "openai":
    print("\nPROVIDER SKIP SIMULATION: SUCCESS")
else:
    print("\nPROVIDER SKIP SIMULATION: FAILED")
