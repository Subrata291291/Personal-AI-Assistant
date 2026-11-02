
from app.workflows.chat_graph import chat_graph


thread_config = {
    "configurable": {
        "thread_id": "checkpoint-demo-001"
    }
}


first_state = {
    "user_id": "manual-user",
    "conversation_id": "checkpoint-demo-001",
    "user_message": "My name is Rahul.",
    "assistant_response": "",
    "provider": "",
    "model": "",
}


print("\n========== FIRST REQUEST ==========")

first_result = chat_graph.invoke(
    first_state,
    config=thread_config,
)

print("User:", first_result["user_message"])
print("Assistant:", first_result["assistant_response"])
print("Provider:", first_result["provider"])


print("\n========== SECOND REQUEST ==========")

second_result = chat_graph.invoke(
    {
        **first_result,
        "user_message": "I like learning artificial intelligence.",
    },
    config=thread_config,
)

print("User:", second_result["user_message"])
print("Assistant:", second_result["assistant_response"])


print("\n========== CHECKPOINT VERIFICATION ==========")

snapshot = chat_graph.get_state(thread_config)

print("Thread ID:", thread_config["configurable"]["thread_id"])
print("Saved User Message:", snapshot.values["user_message"])
print("Saved Provider:", snapshot.values["provider"])

if snapshot.values["user_message"] == (
    "I like learning artificial intelligence."
):
    print("\nCHECKPOINT PERSISTENCE: SUCCESS")
else:
    print("\nCHECKPOINT PERSISTENCE: FAILED")
