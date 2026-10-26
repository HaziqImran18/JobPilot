"""Embedding backends.

Three providers are supported:

* ``openai``  – best quality, costs a fraction of a cent per job.
* ``local``   – sentence-transformers, fully offline (optional dependency).
* ``hashing`` – dependency-free feature hashing, always available.

The hashing backend is intentionally simple: it gives deterministic vectors so
the app, tests and evals work without an API key. It is not a semantic model,
but it is good enough for lexical similarity and offline evaluation.
"""

from __future__ import annotations

import hashlib
import math
import re
from abc import ABC, abstractmethod

import numpy as np

from jobpilot.core.config import get_settings

_STOPWORDS = {
    "the",
    "a",
    "an",
    "and",
    "or",
    "but",
    "if",
    "then",
    "else",
    "for",
    "of",
    "to",
    "in",
    "on",
    "at",
    "by",
    "with",
    "from",
    "as",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "being",
    "this",
    "that",
    "these",
    "those",
    "it",
    "its",
    "we",
    "you",
    "your",
    "our",
    "they",
    "their",
    "he",
    "she",
    "his",
    "her",
    "i",
    "me",
    "my",
    "will",
    "would",
    "can",
    "could",
    "should",
    "may",
    "might",
    "must",
    "do",
    "does",
    "did",
    "have",
    "has",
    "had",
    "not",
    "no",
    "so",
    "than",
    "too",
    "very",
    "just",
    "about",
    "into",
    "over",
    "under",
    "also",
    "more",
    "most",
    "some",
    "any",
    "all",
    "each",
    "other",
    "such",
    "what",
    "which",
    "who",
    "whom",
    "when",
    "where",
    "why",
    "how",
    "there",
    "here",
    "up",
    "out",
    "off",
    "per",
    "via",
    "using",
    "use",
    "used",
    "work",
    "working",
    "role",
    "job",
    "team",
}

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9+#.\-]*")


def tokenize(text: str) -> list[str]:
    tokens = [t.strip(".-") for t in _TOKEN_RE.findall((text or "").lower())]
    return [t for t in tokens if t and t not in _STOPWORDS and len(t) > 1]


def _stable_bucket(token: str, dim: int) -> tuple[int, float]:
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    value = int.from_bytes(digest, "little")
    return value % dim, 1.0 if (value >> 63) & 1 else -1.0


def _l2_normalize(vector: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vector))
    if norm <= 1e-12:
        return vector
    return vector / norm


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    if a.size == 0 or b.size == 0 or a.shape != b.shape:
        return 0.0
    denominator = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denominator <= 1e-12:
        return 0.0
    return float(np.dot(a, b) / denominator)


class Embedder(ABC):
    identifier: str
    dim: int

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one unit-length vector per input text."""

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]


class HashingEmbedder(Embedder):
    def __init__(self, dim: int = 256) -> None:
        self.dim = max(64, dim)
        self.identifier = f"hashing:{self.dim}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        output: list[list[float]] = []
        for text in texts:
            vector = np.zeros(self.dim, dtype=np.float32)
            tokens = tokenize(text)
            features = tokens + [f"{a}_{b}" for a, b in zip(tokens, tokens[1:], strict=False)]
            for token in features:
                bucket, sign = _stable_bucket(token, self.dim)
                vector[bucket] += sign
            output.append(_l2_normalize(vector).tolist())
        return output


class OpenAIEmbedder(Embedder):
    def __init__(self) -> None:
        from openai import OpenAI

        settings = get_settings()
        self._client = OpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url or None,
            timeout=settings.request_timeout,
        )
        self._model = settings.embedding_model
        self.identifier = f"openai:{self._model}"
        self.dim = 0  # learned from the first response

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        batch_size = 64
        for start in range(0, len(texts), batch_size):
            batch = [text[:8000] for text in texts[start : start + batch_size]]
            response = self._client.embeddings.create(model=self._model, input=batch)
            vectors.extend(
                _l2_normalize(np.asarray(item.embedding, dtype=np.float32)).tolist()
                for item in response.data
            )
        if vectors:
            self.dim = len(vectors[0])
        return vectors


class LocalEmbedder(Embedder):
    def __init__(self) -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "Local embeddings require `pip install -e '.[local-embeddings]'`."
            ) from exc
        self._model = SentenceTransformer("all-MiniLM-L6-v2")
        self.identifier = "local:all-MiniLM-L6-v2"
        self.dim = int(self._model.get_sentence_embedding_dimension())

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = self._model.encode(texts, normalize_embeddings=True)
        return [np.asarray(vector, dtype=np.float32).tolist() for vector in vectors]


class ResilientEmbedder(Embedder):
    """Tries the configured provider, falls back to hashing if it is unavailable."""

    def __init__(self) -> None:
        settings = get_settings()
        provider = settings.resolved_embedding_provider
        self._fallback = HashingEmbedder(settings.embedding_dim)
        self._backend: Embedder | None = None
        self._error = ""
        try:
            if provider == "openai":
                self._backend = OpenAIEmbedder()
            elif provider == "local":
                self._backend = LocalEmbedder()
            else:
                self._backend = self._fallback
        except Exception as exc:  # pragma: no cover - defensive
            self._error = str(exc)
            self._backend = self._fallback
        effective = self._backend or self._fallback
        self.identifier = effective.identifier
        self.dim = effective.dim

    @property
    def degraded(self) -> bool:
        return self._backend is self._fallback and self._error != ""

    def embed(self, texts: list[str]) -> list[list[float]]:
        assert self._backend is not None
        try:
            vectors = self._backend.embed(texts)
            self.dim = len(vectors[0]) if vectors else self.dim
            return vectors
        except Exception:
            self._backend = self._fallback
            self.identifier = self._fallback.identifier
            self.dim = self._fallback.dim
            return self._fallback.embed(texts)


_embedder: Embedder | None = None


def get_embedder() -> Embedder:
    global _embedder
    if _embedder is None:
        _embedder = ResilientEmbedder()
    return _embedder


def vector_to_blob(vector: list[float] | np.ndarray) -> bytes:
    return np.asarray(vector, dtype=np.float32).tobytes()


def blob_to_vector(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


def unit_vector(vector: list[float] | np.ndarray) -> np.ndarray:
    array = np.asarray(vector, dtype=np.float32)
    return _l2_normalize(array)


def token_overlap(query: str, text: str) -> float:
    """Jaccard-ish overlap used by the offline matcher."""
    query_tokens = set(tokenize(query))
    text_tokens = set(tokenize(text))
    if not query_tokens or not text_tokens:
        return 0.0
    return len(query_tokens & text_tokens) / math.sqrt(len(query_tokens) * len(text_tokens))


__all__ = [
    "Embedder",
    "HashingEmbedder",
    "LocalEmbedder",
    "OpenAIEmbedder",
    "ResilientEmbedder",
    "blob_to_vector",
    "cosine_similarity",
    "get_embedder",
    "token_overlap",
    "tokenize",
    "unit_vector",
    "vector_to_blob",
]
