from app.memory.classifier.memory_classifier import MemoryClassifier


TEST_CASES = [
    {
        "text": "I prefer Bengali explanations.",
        "expected": "long_term",
    },
    {
        "text": "Yesterday I attended an AI interview.",
        "expected": "episodic",
    },
    {
        "text": "My current project uses FastAPI and PostgreSQL.",
        "expected": "semantic",
    },
    {
        "text": "I review my emails every morning.",
        "expected": "procedural",
    },
    {
        "text": "Hello, how are you?",
        "expected": None,
    },
]


def main():
    classifier = MemoryClassifier()

    passed = 0

    print("\nAI MEMORY CLASSIFICATION VERIFICATION")
    print("=" * 55)

    for index, test in enumerate(TEST_CASES, start=1):
        text = test["text"]
        expected = test["expected"]

        print(f"\nTest {index}: {text}")

        try:
            result = classifier.classify(text)

            print("Should store:", result.should_store)
            print("Memory type:", result.memory_type)
            print("Confidence:", result.confidence)
            print("Reason:", result.reason)
            print(
                "Sensitive:",
                result.contains_sensitive_information,
            )

            actual = (
                result.memory_type
                if result.should_store
                else None
            )

            if actual == expected:
                passed += 1
                print("CLASSIFICATION: PASS")
            else:
                print("CLASSIFICATION: NEEDS REVIEW")
                print("Expected:", expected)
                print("Actual:", actual)

        except Exception as error:
            print("CLASSIFICATION ERROR:", type(error).__name__)
            print("Message:", str(error))

    print("\n" + "=" * 55)
    print(f"RESULT: {passed}/{len(TEST_CASES)} passed")
    print("=" * 55)


if __name__ == "__main__":
    main()
