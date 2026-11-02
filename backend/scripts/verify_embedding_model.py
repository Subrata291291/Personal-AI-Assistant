
import math

from google import genai
from google.genai import types

from app.core.config import settings


MODEL = "gemini-embedding-2"
DIMENSION = 768

def main():
    client = genai.Client(
        api_key=settings.GEMINI_API_KEY.get_secret_value()
    )

    response = client.models.embed_content(
        model=MODEL,
        contents="The user prefers step-by-step explanations in Bengali.",
        config=types.EmbedContentConfig(
            output_dimensionality=DIMENSION
        ),
    )

    embedding = response.embeddings[0].values

    vector_length = len(embedding)
    vector_norm = math.sqrt(sum(value * value for value in embedding))

    print("Embedding model:", MODEL)
    print("Expected dimensions:", DIMENSION)
    print("Actual dimensions:", vector_length)
    print("Vector norm:", round(vector_norm, 6))

    assert vector_length == DIMENSION, "Dimension mismatch!"
    assert all(math.isfinite(value) for value in embedding), (
        "Embedding contains invalid numbers!"
    )

    print("\nEmbedding verification: PASS")


if __name__ == "__main__":
    main()
