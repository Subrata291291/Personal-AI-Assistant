
from app.workflows.chat_graph import chat_graph

# Same thread_id means the same conversation checkpoint.
thread_config = {
    "configurable": {
        "thread_id": "conversation-history-demo-001"
    }
}

# First message: introduce a name.
first_state = {
    "user_id": "manual-verification-user",
    "conversation_id": "conversation-history-demo-001",
    "user_message": "My name is Rahul.",
    "assistant_response": "",
    "provider": "",
    "model": "",
    "messages": [],
}

print("\n--- FIRST MESSAGE ---")
first_result = chat_graph.invoke(first_state, config=thread_config)

print("Assistant:", first_result["assistant_response"])
print("Provider:", first_result["provider"])

# Second message: ask about the earlier information.
# Only send the new message. LangGraph restores the previous state.
print("\n--- SECOND MESSAGE ---")
second_result = chat_graph.invoke(
    {"user_message": "What is my name?"},
    config=thread_config,
)

print("Assistant:", second_result["assistant_response"])
print("Provider:", second_result["provider"])

# Inspect the saved graph state.
snapshot = chat_graph.get_state(thread_config)
saved_messages = snapshot.values["messages"]

print("\n--- SAVED CONVERSATION HISTORY ---")
for index, message in enumerate(saved_messages, start=1):
    print(f"{index}. {message['role']}: {message['content']}")

print("\nTotal messages:", len(saved_messages))

# Verify that the second response used the earlier context.
if "rahul" in second_result["assistant_response"].lower():
    print("\nCONVERSATION HISTORY: PASS")
else:
    print("\nCONVERSATION HISTORY: NEEDS REVIEW")
