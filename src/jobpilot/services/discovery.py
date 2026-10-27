"""One discovery pipeline shared by the board, the API and the agent.

Every caller wants the same thing: interpret the request, fetch live postings
when the cached snapshot is stale, apply the structured filters, and report
honestly which sources worked. Keeping that in one service is what stops the
conversational agent and the job board from drifting apart.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from jobpilot.core.db import Database, get_db
from jobpilot.core.models import JobQuery
from jobpilot.core.utils import json_dumps, json_loads, normalize_text, stable_hash, utc_now_iso
from jobpilot.pipelines.intent import SearchIntent, interpret_query
from jobpilot.services.jobs import JobService

DEFAULT_TTL_SECONDS = 600
FAILURE_TTL_SECONDS = 90

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(signature: str) -> threading.Lock:
    with _locks_guard:
        lock = _locks.get(signature)
        if lock is None:
            lock = threading.Lock()
            _locks[signature] = lock
        return lock


@dataclass
class DiscoveryResult:
    items: list[dict[str, Any]] = field(default_factory=list)
    total: int = 0
    exact: int = 0
    related: int = 0
    sources: dict[str, int] = field(default_factory=dict)
    source_status: list[dict[str, Any]] = field(default_factory=list)
    interpreted: dict[str, Any] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    cached: bool = False
    ok: bool = True
    fetched: int = 0
    stored: int = 0
    duration_ms: int = 0
    retrieved_at: str = ""
    message: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "items": self.items,
            "total": self.total,
            "exact": self.exact,
            "related": self.related,
            "sources": self.sources,
            "source_status": self.source_status,
            "interpreted": self.interpreted,
            "errors": self.errors,
            "cached": self.cached,
            "ok": self.ok,
            "fetched": self.fetched,
            "stored": self.stored,
            "duration_ms": self.duration_ms,
            "retrieved_at": self.retrieved_at,
            "message": self.message,
        }


class DiscoveryService:
    def __init__(
        self,
        db: Database | None = None,
        jobs: JobService | None = None,
    ) -> None:
        self.db = db or get_db()
        self.jobs = jobs or JobService(self.db)

    # ------------------------------------------------------------------- public
    def discover(
        self,
        query: JobQuery,
        *,
        force: bool = False,
        ttl: int = DEFAULT_TTL_SECONDS,
        profile_version: str = "",
        sort: str = "relevance",
        min_score: float | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> DiscoveryResult:
        intent = interpret_query(
            query=query.query,
            location=query.location,
            remote=query.remote,
            category=query.category,
            seniority=query.seniority,
            workplace_type=query.workplace_type,
            employment_type=query.employment_type,
            posted_within_days=query.posted_within_days,
            sources=query.sources,
            include_related=query.include_related,
        )
        signature = self._signature(intent)
        run = None if force else self._cached_run(signature)
        fetched = 0
        stored = 0
        errors: dict[str, str] = {}
        counts: dict[str, int] = {}
        duration_ms = 0
        did_fetch = False
        saved_status: list[dict[str, Any]] | None = None

        if run is None:
            with _lock_for(signature):
                # A second request may have refreshed while we waited.
                run = None if force else self._cached_run(signature)
                if run is None:
                    report = self.jobs.refresh(self._retrieval_query(query, intent))
                    fetched = int(report.get("fetched") or 0)
                    stored = int(report.get("stored") or 0)
                    errors = dict(report.get("errors") or {})
                    counts = dict(report.get("counts") or {})
                    duration_ms = int(report.get("duration_ms") or 0)
                    self._save_run(signature, intent, report, ttl=ttl)
                    run = self._cached_run(signature, ignore_expiry=True)
                    did_fetch = True

        if run is not None:
            payload = json_loads(run["report"], {}) or {}
            errors = dict(payload.get("errors") or errors)
            counts = dict(payload.get("counts") or counts)
            fetched = int(payload.get("fetched") or fetched)
            stored = int(payload.get("stored") or stored)
            duration_ms = int(payload.get("duration_ms") or duration_ms)
            saved_status = payload.get("source_status") or None
            retrieved_at = payload.get("retrieved_at") or run["created_at"]
            interpreted = json_loads(run["filters"], {}) or intent.as_dict()
        else:
            retrieved_at = utc_now_iso()
            interpreted = intent.as_dict()

        page = self.jobs.list_cards(
            profile_version=profile_version,
            query=intent.search_query or query.query,
            location=intent.location,
            remote=query.remote,
            category=intent.category,
            workplace_type=intent.workplace_type or query.workplace_type,
            seniority=intent.seniority,
            employment_type=intent.employment_type,
            skills=query.skills,
            include_related=query.include_related,
            posted_within_days=query.posted_within_days,
            sources=query.sources,
            sort=sort,
            min_score=min_score,
            limit=limit,
            offset=offset,
        )
        status = saved_status or self._source_status(counts, errors)
        ok = bool(page["total"]) or fetched > 0
        message = ""
        if not ok:
            if errors:
                message = (
                    "Live retrieval failed for every enabled source. "
                    "Check the source status panel for the reason."
                )
            else:
                message = (
                    "The live sources returned no postings for this request. "
                    "Try a broader role or location."
                )
        elif fetched == 0 and page["total"] == 0:
            message = "No stored postings match these filters yet."

        return DiscoveryResult(
            items=page["items"],
            total=int(page["total"]),
            exact=int(page.get("exact") or 0),
            related=int(page.get("related") or 0),
            sources=page.get("sources") or {},
            source_status=status,
            interpreted=interpreted,
            errors=errors,
            cached=not did_fetch,
            ok=ok,
            fetched=fetched,
            stored=stored,
            duration_ms=duration_ms,
            retrieved_at=retrieved_at,
            message=message,
        )

    def status(self) -> dict[str, Any]:
        """Latest known source state without doing any network work.

        The shape mirrors a live discovery run (``state`` + ``count``) so the
        board can render stored snapshots and fresh retrievals identically.
        """
        rows = self.db.query(
            "SELECT name, ok, detail, latency_ms, checked_at FROM source_health ORDER BY name"
        )
        sources: list[dict[str, Any]] = []
        for row in rows:
            detail = row["detail"] or ""
            ok = bool(row["ok"])
            count = None
            if ok:
                match = re.match(r"^\s*(\d+)\s+results?\b", detail)
                count = int(match.group(1)) if match else 0
            sources.append(
                {
                    "name": row["name"],
                    "ok": ok,
                    "state": "ok" if ok and count else "empty" if ok else "error",
                    "count": count,
                    "error": "" if ok else detail,
                    "detail": detail,
                    "latency_ms": row["latency_ms"],
                    "checked_at": row["checked_at"],
                }
            )
        return {"sources": sources, "stats": self.jobs.stats()}

    # ----------------------------------------------------------------- helpers
    @staticmethod
    def _signature(intent: SearchIntent) -> str:
        return stable_hash("discovery", intent.signature, length=32)

    @staticmethod
    def _retrieval_query(query: JobQuery, intent: SearchIntent) -> JobQuery:
        return query.model_copy(
            update={
                "query": intent.search_query or query.query,
                "location": intent.resolved_location.display or query.location,
                "category": intent.category or query.category,
                "workplace_type": intent.workplace_type or query.workplace_type,
            }
        )

    def _cached_run(self, signature: str, *, ignore_expiry: bool = False) -> Any:
        row = self.db.query_one("SELECT * FROM search_runs WHERE signature = ?", (signature,))
        if row is None:
            return None
        if ignore_expiry:
            return row
        now = utc_now_iso()
        if row["expires_at"] and row["expires_at"] >= now:
            return row
        return None

    def _save_run(
        self,
        signature: str,
        intent: SearchIntent,
        report: dict[str, Any],
        *,
        ttl: int,
    ) -> None:
        now = datetime.now(UTC)
        failed = int(report.get("fetched") or 0) == 0
        seconds = min(ttl, FAILURE_TTL_SECONDS) if failed else ttl
        payload = dict(report)
        payload.setdefault("retrieved_at", now.isoformat(timespec="seconds"))
        self.db.execute(
            """INSERT INTO search_runs(signature, query, filters, report, total, created_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(signature) DO UPDATE SET query = excluded.query,
               filters = excluded.filters, report = excluded.report, total = excluded.total,
               created_at = excluded.created_at, expires_at = excluded.expires_at""",
            (
                signature,
                normalize_text(intent.search_query),
                json_dumps(intent.as_dict()),
                json_dumps(payload),
                int(report.get("stored") or 0),
                now.isoformat(timespec="seconds"),
                (now + timedelta(seconds=seconds)).isoformat(timespec="seconds"),
            ),
        )

    @staticmethod
    def _source_status(
        counts: dict[str, int], errors: dict[str, str]
    ) -> list[dict[str, Any]]:
        names = sorted(set(counts) | set(errors))
        status: list[dict[str, Any]] = []
        for name in names:
            error = errors.get(name) or ""
            count = int(counts.get(name) or 0)
            if error:
                state = "error"
            elif count:
                state = "ok"
            else:
                state = "empty"
            status.append(
                {
                    "name": name,
                    "ok": not error and count > 0,
                    "state": state,
                    "count": count,
                    "error": error,
                }
            )
        return status


def get_discovery() -> DiscoveryService:
    return DiscoveryService()


__all__ = ["DEFAULT_TTL_SECONDS", "DiscoveryResult", "DiscoveryService", "get_discovery"]
