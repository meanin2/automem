"""Google Gemini embedding provider using Gemini API."""

import logging
from typing import List, Optional

from google import genai
from google.genai import types

from automem.embedding.provider import EmbeddingProvider

logger = logging.getLogger(__name__)

# Gemini embedding model
GEMINI_EMBEDDING_MODEL = "gemini-embedding-001"

# Supported task types for different use cases
GEMINI_TASK_TYPES = {
    "semantic_similarity": "SEMANTIC_SIMILARITY",
    "classification": "CLASSIFICATION",
    "clustering": "CLUSTERING",
    "retrieval_document": "RETRIEVAL_DOCUMENT",
    "retrieval_query": "RETRIEVAL_QUERY",
    "code_retrieval_query": "CODE_RETRIEVAL_QUERY",
    "question_answering": "QUESTION_ANSWERING",
    "fact_verification": "FACT_VERIFICATION",
}


class GeminiEmbeddingProvider(EmbeddingProvider):
    """Generates embeddings using Google's Gemini embedding API.

    Requires a Gemini API key and makes network requests to Google.
    Provides high-quality semantic embeddings with flexible dimensionality.
    Supports task-specific optimization for better retrieval performance.
    """

    def __init__(
        self,
        api_key: str,
        model: str = GEMINI_EMBEDDING_MODEL,
        dimension: int = 768,
        task_type: Optional[str] = None,
    ):
        """Initialize Gemini embedding provider.

        Args:
            api_key: Google Gemini API key
            model: Gemini embedding model to use (default: gemini-embedding-001)
            dimension: Number of dimensions for embeddings (128-3072, recommended: 768, 1536, 3072)
            task_type: Optional task type for optimized embeddings. Options:
                - semantic_similarity: For comparing text similarity
                - retrieval_document: For indexing documents
                - retrieval_query: For search queries
                - classification: For text classification
                - clustering: For clustering tasks
                - code_retrieval_query: For code search
                - question_answering: For QA systems
                - fact_verification: For fact checking

        Raises:
            Exception: If Gemini client initialization fails
        """
        self.client = genai.Client(api_key=api_key)
        self.model = model
        self._dimension = dimension

        # Validate and set task type
        if task_type:
            task_type_lower = task_type.lower()
            if task_type_lower in GEMINI_TASK_TYPES:
                self.task_type = GEMINI_TASK_TYPES[task_type_lower]
            elif task_type.upper() in GEMINI_TASK_TYPES.values():
                self.task_type = task_type.upper()
            else:
                logger.warning(
                    "Unknown task_type '%s', using default (no task type). "
                    "Valid options: %s",
                    task_type,
                    list(GEMINI_TASK_TYPES.keys()),
                )
                self.task_type = None
        else:
            # Default to RETRIEVAL_DOCUMENT for memory storage use case
            self.task_type = "RETRIEVAL_DOCUMENT"

        logger.info(
            "Gemini embedding provider initialized (model=%s, dimensions=%d, task_type=%s)",
            model,
            dimension,
            self.task_type or "default",
        )

    def _normalize_embedding(self, embedding: List[float]) -> List[float]:
        """Normalize embedding vector to unit length.

        Gemini's 3072d embeddings are normalized, but smaller dimensions need
        normalization for accurate cosine similarity comparisons.

        Args:
            embedding: Raw embedding vector

        Returns:
            Normalized embedding vector
        """
        if self._dimension == 3072:
            # 3072d embeddings are already normalized
            return embedding

        import math
        norm = math.sqrt(sum(x * x for x in embedding))
        if norm == 0:
            return embedding
        return [x / norm for x in embedding]

    def generate_embedding(self, text: str) -> List[float]:
        """Generate an embedding using Gemini API.

        Args:
            text: The text to embed

        Returns:
            Embedding vector from Gemini

        Raises:
            Exception: If API call fails
        """
        config = types.EmbedContentConfig(output_dimensionality=self._dimension)
        if self.task_type:
            config.task_type = self.task_type

        result = self.client.models.embed_content(
            model=self.model,
            contents=text,
            config=config,
        )

        # Gemini returns a list of embeddings, get the first one
        embedding = result.embeddings[0].values

        if len(embedding) != self._dimension:
            raise ValueError(
                f"Gemini embedding length {len(embedding)} != configured dimension {self._dimension} "
                f"(model={self.model}). Check output_dimensionality setting."
            )

        # Normalize for smaller dimensions
        embedding = self._normalize_embedding(list(embedding))

        logger.debug("Generated Gemini embedding for text (length: %d)", len(text))
        return embedding

    def generate_embeddings_batch(self, texts: List[str]) -> List[List[float]]:
        """Generate embeddings for multiple texts in one API call.

        Args:
            texts: List of texts to embed

        Returns:
            List of embedding vectors from Gemini

        Raises:
            Exception: If API call fails
        """
        if not texts:
            return []

        config = types.EmbedContentConfig(output_dimensionality=self._dimension)
        if self.task_type:
            config.task_type = self.task_type

        result = self.client.models.embed_content(
            model=self.model,
            contents=texts,
            config=config,
        )

        embeddings = [list(emb.values) for emb in result.embeddings]

        # Validate dimensions
        bad = next((i for i, e in enumerate(embeddings) if len(e) != self._dimension), None)
        if bad is not None:
            raise ValueError(
                f"Gemini batch embedding length {len(embeddings[bad])} != configured dimension {self._dimension} "
                f"at index {bad} (model={self.model})."
            )

        # Normalize all embeddings
        embeddings = [self._normalize_embedding(emb) for emb in embeddings]

        logger.info(
            "Generated %d Gemini embeddings in batch (avg length: %d)",
            len(embeddings),
            sum(len(t) for t in texts) // len(texts) if texts else 0,
        )
        return embeddings

    def dimension(self) -> int:
        """Return embedding dimensionality.

        Returns:
            The number of dimensions in the embedding vectors
        """
        return self._dimension

    def provider_name(self) -> str:
        """Return provider name.

        Returns:
            Provider identifier
        """
        return f"gemini:{self.model}"
