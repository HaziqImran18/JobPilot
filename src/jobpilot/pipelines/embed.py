"""Embedding persistence helpers for jobs and profiles."""

from __future__ import annotations

import numpy as np

from jobpilot.core.db import Database
from jobpilot.memory.embeddings import get_embedder, unit_vector, vector_to_blob


def ensure_vector(db: Database, owner_type: str, owner_id: str, text: str) -> np.ndarray:
    """Return a cached embedding, computing and storing it on first use."""
    embedder = get_embedder()
    row = db.query_one(
        "SELECT vector FROM vectors WHERE owner_type = ? AND owner_id = ? AND model = ?",
        (owner_type, owner_id, embedder.identifier),
    )
    if row:
        return np.frombuffer(row["vector"], dtype=np.float32)
    vector = unit_vector(embedder.embed_one(text))
    db.execute(
        "INSERT INTO vectors(owner_type, owner_id, model, dim, vector) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(owner_type, owner_id, model) DO UPDATE SET dim = excluded.dim, vector = excluded.vector",
        (owner_type, owner_id, embedder.identifier, len(vector), vector_to_blob(vector)),
    )
    return vector


def job_embedding_text(job) -> str:
    return " ".join(
        [
            job.title,
            job.company,
            job.location,
            " ".join(job.tags),
            (job.description or "")[:5000],
        ]
    )


def job_vector_id(job) -> str:
    from jobpilot.core.utils import stable_hash

    return f"{job.id}:{stable_hash(job.title, job.description, length=10)}"


def ensure_vectors_many(
    db: Database, owner_type: str, entries: list[tuple[str, str]]
) -> dict[str, np.ndarray]:
    """Batch version of :func:`ensure_vector` (one embedding request per batch)."""
    if not entries:
        return {}
    embedder = get_embedder()
    result: dict[str, np.ndarray] = {}
    missing: list[tuple[str, str]] = []
    ids = [owner_id for owner_id, _ in entries]
    placeholders = ",".join("?" for _ in ids)
    rows = db.query(
        f"SELECT owner_id, vector FROM vectors WHERE owner_type = ? AND model = ? AND owner_id IN ({placeholders})",  # noqa: S608
        (owner_type, embedder.identifier, *ids),
    )
    for row in rows:
        result[row["owner_id"]] = np.frombuffer(row["vector"], dtype=np.float32)
    for owner_id, text in entries:
        if owner_id not in result:
            missing.append((owner_id, text))
    if missing:
        vectors = embedder.embed([text for _, text in missing])
        from jobpilot.memory.embeddings import vector_to_blob

        for (owner_id, _), vector in zip(missing, vectors, strict=False):
            normalized = unit_vector(vector)
            result[owner_id] = normalized
            db.execute(
                "INSERT INTO vectors(owner_type, owner_id, model, dim, vector) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(owner_type, owner_id, model) DO UPDATE SET dim = excluded.dim, vector = excluded.vector",
                (
                    owner_type,
                    owner_id,
                    embedder.identifier,
                    len(normalized),
                    vector_to_blob(normalized),
                ),
            )
    return result


__all__ = ["ensure_vector", "ensure_vectors_many", "job_embedding_text", "job_vector_id"]
