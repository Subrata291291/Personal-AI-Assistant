from app.memory.classifier.memory_classifier import MemoryClassification
from app.memory.classifier.memory_policy import MemoryPolicy


policy = MemoryPolicy(min_confidence=0.80)

test_cases = [
    {
        "name": "Valid semantic memory",
        "content": "My project uses FastAPI and PostgreSQL.",
        "classification": MemoryClassification(
            should_store=True,
            memory_type="semantic",
            confidence=0.95,
            reason="User-specific project fact.",
            contains_sensitive_information=False,
        ),
        "expected": True,
    },
    {
        "name": "Low confidence",
        "content": "I may prefer Python.",
        "classification": MemoryClassification(
            should_store=True,
            memory_type="long_term",
            confidence=0.65,
            reason="Uncertain preference.",
            contains_sensitive_information=False,
        ),
        "expected": False,
    },
    {
        "name": "Sensitive information",
        "content": "My private information.",
        "classification": MemoryClassification(
            should_store=True,
            memory_type="semantic",
            confidence=0.99,
            reason="Sensitive content.",
            contains_sensitive_information=True,
        ),
        "expected": False,
    },
    {
        "name": "Classifier rejected storage",
        "content": "Hello, how are you?",
        "classification": MemoryClassification(
            should_store=False,
            memory_type=None,
            confidence=1.0,
            reason="Ordinary greeting.",
            contains_sensitive_information=False,
        ),
        "expected": False,
    },
    {
        "name": "Empty content",
        "content": "   ",
        "classification": MemoryClassification(
            should_store=True,
            memory_type="episodic",
            confidence=0.95,
            reason="Test empty content.",
            contains_sensitive_information=False,
        ),
        "expected": False,
    },
    {
        "name": "Invalid memory category",
        "content": "Some user-specific information.",
        "classification": MemoryClassification.model_construct(
            should_store=True,
            memory_type="unknown",
            confidence=0.95,
            reason="Invalid category test.",
            contains_sensitive_information=False,
        ),
        "expected": False,
    },
]


def main():
    passed = 0

    print("\nMEMORY POLICY VERIFICATION")
    print("=" * 60)

    for index, test in enumerate(test_cases, start=1):
        decision = policy.evaluate(
            content=test["content"],
            classification=test["classification"],
        )

        success = decision.allowed == test["expected"]

        print(f"\nTest {index}: {test['name']}")
        print("Allowed:", decision.allowed)
        print("Reason:", decision.reason)
        print("Expected:", test["expected"])
        print("Result:", "PASS" if success else "FAIL")

        if success:
            passed += 1

    print("\n" + "=" * 60)
    print(f"RESULT: {passed}/{len(test_cases)} passed")
    print("=" * 60)


if __name__ == "__main__":
    main()
