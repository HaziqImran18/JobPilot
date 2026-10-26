"""Semantic and episodic memory backed by SQLite plus embeddings."""

from __future__ import annotations

import math
import uuid
from datetime import datetime
from typing import Any

import numpy as np

from jobpilot.core.db import Database, get_db
from jobpilot.core.models import Memory, MemoryKind, MemoryStatus
from jobpilot.core.utils import json_dumps, json_loads, normalize_text, utc_now, utc_now_iso
from jobpilot.memory.embeddings import (
    Embedder,
    blob_to_vector,
    cosine_similarity,
    get_embedder,
    unit_vector,
    vector_to_blob,
)

MERGE_THRESHOLD = 0.93


class MemoryStore:
    def __init__(self, db: Database | None = None, embedder: Embedder | None = None) -> None:
        self.db = db or get_db()
        self.embedder = embedder or get_embedder()

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _row_to_memory(row: Any) -> Memory:
        return Memory(
            id=row["id"],
            kind=MemoryKind(row["kind"]),
            text=row["text"],
            key=row["key"],
            value=row["value"],
            importance=float(row["importance"]),
            confidence=float(row["confidence"]),
            provenance=json_loads(row["provenance"], {}) or {},
            status=MemoryStatus(row["status"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_used_at=row["last_used_at"],
            use_count=int(row["use_count"]),
        )

    def _save_vector(self, memory_id: str, vector: list[float]) -> None:
        self.db.execute(
            "INSERT INTO vectors(owner_type, owner_id, model, dim, vector) VALUES ('memory', ?, ?, ?, ?) "
            "ON CONFLICT(owner_type, owner_id, model) DO UPDATE SET dim = excluded.dim, vector = excluded.vector",
            (memory_id, self.embedder.identifier, len(vector), vector_to_blob(vector)),
        )

    def _vector_for(self, memory_id: str) -> np.ndarray | None:
        row = self.db.query_one(
            "SELECT vector FROM vectors WHERE owner_type = 'memory' AND owner_id = ? AND model = ?",
            (memory_id, self.embedder.identifier),
        )
        return blob_to_vector(row["vector"]) if row else None

    # -------------------------------------------------------------------- write
    def add(
        self,
        text: str,
        *,
        kind: MemoryKind = MemoryKind.semantic,
        key: str = "",
        value: str = "",
        importance: float = 0.5,
        confidence: float = 0.8,
        provenance: dict[str, Any] | None = None,
        status: MemoryStatus = MemoryStatus.active,
    ) -> tuple[Memory, str]:
        """Persist a memory, merging near-duplicates.

        Returns ``(memory, action)`` where action is created, merged, duplicate
        or conflict. Conflicts are stored as ``needs_confirmation`` instead of
        overwriting history.
        """
        text = text.strip()
        if not text:
            raise ValueError("Memory text cannot be empty.")
        provenance = provenance or {}
        existing = self.db.query(
            "SELECT * FROM memories WHERE status = 'active' AND kind = ? ORDER BY created_at DESC",
            (kind.value,),
        )
        vector = unit_vector(self.embedder.embed_one(text))

        if key:
            same_key = next((row for row in existing if row["key"] == key), None)
            if same_key is not None:
                same_value = normalize_text(
                    same_key["value"] or same_key["text"]
                ) == normalize_text(value or text)
                if same_value:
                    return self._merge(
                        same_key, text, importance, confidence, provenance
                    ), "duplicate"
                status = MemoryStatus.needs_confirmation
                provenance = {**provenance, "conflicts_with": same_key["id"]}

        for row in existing:
            stored = self._vector_for(row["id"])
            if stored is None:
                continue
            similarity = cosine_similarity(vector, stored)
            if similarity >= MERGE_THRESHOLD and not (
                key and row["key"] == key and normalize_text(row["value"]) != normalize_text(value)
            ):
                return self._merge(
                    row, text, importance, confidence, provenance, similarity
                ), "merged"

        memory_id = f"mem_{uuid.uuid4().hex[:12]}"
        now = utc_now_iso()
        self.db.execute(
            "INSERT INTO memories(id, kind, text, key, value, importance, confidence, provenance, status, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                memory_id,
                kind.value,
                text,
                key,
                value,
                max(0.0, min(1.0, importance)),
                max(0.0, min(1.0, confidence)),
                json_dumps(provenance),
                status.value,
                now,
                now,
            ),
        )
        self._save_vector(memory_id, vector.tolist())
        memory = Memory(
            id=memory_id,
            kind=kind,
            text=text,
            key=key,
            value=value,
            importance=importance,
            confidence=confidence,
            provenance=provenance,
            status=status,
            created_at=now,
            updated_at=now,
        )
        return memory, "conflict" if status is MemoryStatus.needs_confirmation else "created"

    def _merge(
        self,
        row: Any,
        text: str,
        importance: float,
        confidence: float,
        provenance: dict[str, Any],
        similarity: float | None = None,
    ) -> Memory:
        old_provenance = json_loads(row["provenance"], {}) or {}
        merged_from = list(old_provenance.get("merged_from", []))
        merged_from.append(
            {
                "text": text,
                "at": utc_now_iso(),
                "similarity": round(similarity, 3) if similarity is not None else None,
            }
        )
        new_provenance = {
            **old_provenance,
            **{k: v for k, v in provenance.items() if k not in {"merged_from"}},
            "merged_from": merged_from[-10:],
        }
        # Duplicate/merge evidence increases confidence, never decreases it.
        new_confidence = min(1.0, max(float(row["confidence"]), confidence) + 0.02)
        new_importance = max(float(row["importance"]), importance)
        self.db.execute(
            "UPDATE memories SET confidence = ?, importance = ?, provenance = ?, updated_at = ? WHERE id = ?",
            (new_confidence, new_importance, json_dumps(new_provenance), utc_now_iso(), row["id"]),
        )
        updated = self.db.query_one("SELECT * FROM memories WHERE id = ?", (row["id"],))
        assert updated is not None
        return self._row_to_memory(updated)

    # --------------------------------------------------------------------- read
    def get(self, memory_id: str) -> Memory | None:
        row = self.db.query_one("SELECT * FROM memories WHERE id = ?", (memory_id,))
        return self._row_to_memory(row) if row else None

    def list(
        self,
        *,
        status: MemoryStatus | None = MemoryStatus.active,
        kind: MemoryKind | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Memory]:
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind.value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.db.query(
            f"SELECT * FROM memories {where} ORDER BY importance DESC, created_at DESC LIMIT ? OFFSET ?",  # noqa: S608
            (*params, limit, offset),
        )
        return [self._row_to_memory(row) for row in rows]

    def _ensure_embeddings(self, rows: list[Any]) -> dict[str, np.ndarray]:
        vectors: dict[str, np.ndarray] = {}
        missing: list[Any] = []
        for row in rows:
            vector = self._vector_for(row["id"])
            if vector is None:
                missing.append(row)
            else:
                vectors[row["id"]] = vector
        if missing:
            embedded = self.embedder.embed([row["text"] for row in missing])
            for row, vector in zip(missing, embedded, strict=False):
                self._save_vector(row["id"], vector)
                vectors[row["id"]] = unit_vector(vector)
        return vectors

    def recall(
        self,
        query: str,
        *,
        k: int = 5,
        kind: MemoryKind | None = None,
        min_similarity: float = 0.18,
        boost_ids: list[str] | None = None,
    ) -> list[Memory]:
        """Rank memories by similarity, recency and importance."""
        if not query.strip():
            return []
        clauses = ["m.status = 'active'"]
        params: list[Any] = []
        if kind is not None:
            clauses.append("m.kind = ?")
            params.append(kind.value)
        rows = self.db.query(
            f"SELECT m.* FROM memories m WHERE {' AND '.join(clauses)}",  # noqa: S608
            tuple(params),
        )
        if not rows:
            return []
        vectors = self._ensure_embeddings(rows)
        query_vector = unit_vector(self.embedder.embed_one(query))
        now = utc_now()
        scored: list[tuple[float, Any, float]] = []
        for row in rows:
            vector = vectors.get(row["id"])
            similarity = cosine_similarity(query_vector, vector) if vector is not None else 0.0
            if similarity < min_similarity:
                continue
            age_days = max((now - datetime.fromisoformat(row["created_at"])).days, 0)
            recency = math.exp(-age_days / 120)
            importance = float(row["importance"])
            score = 0.70 * similarity + 0.18 * recency + 0.12 * importance
            if boost_ids and row["id"] in boost_ids:
                score += 0.15
            scored.append((score, row, similarity))
        scored.sort(key=lambda item: item[0], reverse=True)
        top = scored[:k]
        if top:
            now_iso = utc_now_iso()
            self.db.execute_many(
                "UPDATE memories SET last_used_at = ?, use_count = use_count + 1 WHERE id = ?",
                [(now_iso, row["id"]) for _, row, _ in top],
            )
        memories: list[Memory] = []
        for _, row, similarity in top:
            memory = self._row_to_memory(row)
            memory.similarity = round(similarity, 3)
            memories.append(memory)
        return memories

    def conflicts(self, limit: int = 50) -> list[Memory]:
        return self.list(status=MemoryStatus.needs_confirmation, limit=limit)

    # ------------------------------------------------------------------ control
    def confirm(self, memory_id: str) -> Memory | None:
        self.db.execute(
            "UPDATE memories SET status = 'active', updated_at = ? WHERE id = ?",
            (utc_now_iso(), memory_id),
        )
        return self.get(memory_id)

    def forget(self, memory_id: str, *, hard: bool = False) -> bool:
        row = self.db.query_one("SELECT id FROM memories WHERE id = ?", (memory_id,))
        if not row:
            return False
        if hard:
            self.db.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            self.db.execute(
                "DELETE FROM vectors WHERE owner_type = 'memory' AND owner_id = ?", (memory_id,)
            )
        else:
            self.db.execute(
                "UPDATE memories SET status = 'forgotten', updated_at = ? WHERE id = ?",
                (utc_now_iso(), memory_id),
            )
            self.db.execute(
                "DELETE FROM vectors WHERE owner_type = 'memory' AND owner_id = ?", (memory_id,)
            )
        return True

    def count(self) -> dict[str, int]:
        rows = self.db.query("SELECT status, COUNT(*) AS n FROM memories GROUP BY status")
        return {row["status"]: int(row["n"]) for row in rows}


__all__ = ["MemoryStore", "MERGE_THRESHOLD"]
