
from app.llm.router import llm_router
from app.llm.schemas import LLMMessage, LLMRequest


request = LLMRequest(
    messages=[
        LLMMessage(
            role="system",
            content="You are a helpful personal AI assistant.",
        ),
        LLMMessage(
            role="user",
            content="Explain what an AI personal assistant does in 3 short sentences.",
        ),
    ],
    temperature=0.3,
    max_tokens=200,
)


try:
    response = llm_router.generate(request)

    print("\n========== LLM RESPONSE ==========")
    print("Provider:", response.provider)
    print("Model:", response.model)
    print("Latency:", round(response.latency_ms, 2), "ms")
    print("Response:", response.content)

    if response.usage:
        print("Prompt Tokens:", response.usage.prompt_tokens)
        print("Completion Tokens:", response.usage.completion_tokens)
        print("Total Tokens:", response.usage.total_tokens)

    print("\nLLM REAL API: SUCCESS")

except Exception as error:
    print("\nLLM REAL API: FAILED")
    print("Error Type:", type(error).__name__)
    print("Error:", str(error))
