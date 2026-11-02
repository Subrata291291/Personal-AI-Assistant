import math

from google import genai
from google.genai import types

from app.core.config import settings


EMBEDDING_MODEL = "gemini-embedding-2"
EMBEDDING_DIMENSION = 768


class EmbeddingService:
    """
    Generates and validates text embeddings using Gemini.
    """

    def __init__(self):
        self.client = genai.Client(
            api_key=settings.GEMINI_API_KEY.get_secret_value()
        )

    def embed_text(self, text: str) -> list[float]:
        """
        Convert text into a 768-dimensional embedding vector.
        """

        if not text or not text.strip():
            raise ValueError("Text cannot be empty.")

        response = self.client.models.embed_content(
            model=EMBEDDING_MODEL,
            contents=text,
            config=types.EmbedContentConfig(
                output_dimensionality=EMBEDDING_DIMENSION
            ),
        )

        if not response.embeddings:
            raise RuntimeError("Gemini returned an empty embedding response.")

        embedding = response.embeddings[0].values

        if embedding is None:
            raise RuntimeError("Embedding values are missing.")

        vector = [float(value) for value in embedding]

        if len(vector) != EMBEDDING_DIMENSION:
            raise ValueError(
                f"Expected {EMBEDDING_DIMENSION} dimensions, "
                f"received {len(vector)}."
            )

        if not all(math.isfinite(value) for value in vector):
            raise ValueError("Embedding contains invalid numerical values.")

        return vector