import math

from app.memory.embedding_service import (
    EmbeddingService,
    EMBEDDING_DIMENSION,
    EMBEDDING_MODEL,
)


def main():
    service = EmbeddingService()

    text = "The user prefers step-by-step explanations in Bengali."

    embedding = service.embed_text(text)

    print("Embedding model:", EMBEDDING_MODEL)
    print("Expected dimensions:", EMBEDDING_DIMENSION)
    print("Actual dimensions:", len(embedding))

    vector_norm = math.sqrt(sum(value * value for value in embedding))
    print("Vector norm:", round(vector_norm, 6))

    assert len(embedding) == EMBEDDING_DIMENSION
    assert all(math.isfinite(value) for value in embedding)

    print("\nEmbeddingService verification: PASS")


if __name__ == "__main__":
    main()
