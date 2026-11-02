
from app.workflows.chat_graph import chat_graph


initial_state = {
    "user_id": "manual-verification-user",
    "conversation_id": "manual-verification-conversation",
    "user_message": (
        "Explain how a personal AI assistant "
        "can help me manage my daily activities."
    ),
    "assistant_response": "",
    "provider": "",
    "model": "",
}


try:
    result = chat_graph.invoke(initial_state)

    print("\n========== LANGGRAPH RESULT ==========")

    print("User Message:", result["user_message"])

    print("\nAssistant Response:")
    print(result["assistant_response"])

    print("\nProvider:", result["provider"])
    print("Model:", result["model"])

    if result["assistant_response"] and result["provider"]:
        print("\nLANGGRAPH EXECUTION: SUCCESS")
    else:
        print("\nLANGGRAPH EXECUTION: INCOMPLETE")

except Exception as error:
    print("\nLANGGRAPH EXECUTION: FAILED")
    print("Error Type:", type(error).__name__)
    print("Error:", str(error))
