"""Job storage, search and refresh orchestration."""

from __future__ import annotations

import re
from typing import Any

from jobpilot.core.db import Database, get_db
from jobpilot.core.models import Job, JobCard, JobQuery, Salary
from jobpilot.core.utils import json_dumps, json_loads, money_label, to_iso, utc_now_iso
from jobpilot.pipelines.extract import dedupe, to_job
from jobpilot.sources.registry import FetchReport, SourceRegistry, get_registry


class JobService:
    def __init__(self, db: Database | None = None, registry: SourceRegistry | None = None) -> None:
        self.db = db or get_db()
        self.registry = registry or get_registry()

    # ------------------------------------------------------------------- reads
    def get(self, job_id: str) -> Job | None:
        row = self.db.query_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
        return self._row_to_job(row) if row else None

    def to_card(self, job: Job, score: dict[str, Any] | None = None) -> JobCard:
        salary = job.salary
        return JobCard(
            id=job.id,
            title=job.title,
            company=job.company,
            location=job.location or ("Remote" if job.remote else ""),
            remote=job.remote,
            url=job.url,
            source=job.source,
            salary=money_label(
                salary.minimum if salary else None,
                salary.maximum if salary else None,
                salary.currency if salary else None,
            ),
            posted_at=job.posted_at,
            top_skills=job.tags[:6],
            score=float(score["score"]) if score and score.get("score") is not None else None,
        )

    def search(self, query: JobQuery, *, profile_version: str = "") -> list[Job]:
        clauses, params = self._filters(query)
        sql = f"SELECT j.* FROM jobs j {'WHERE ' + ' AND '.join(clauses) if clauses else ''} "  # noqa: S608
        sql += "ORDER BY j.posted_at DESC, j.fetched_at DESC LIMIT ?"
        rows = self.db.query(sql, (*params, query.limit))
        return [self._row_to_job(row) for row in rows]

    def list_cards(
        self,
        *,
        profile_version: str = "",
        query: str = "",
        location: str = "",
        remote: bool | None = None,
        source: str = "",
        min_score: float | None = None,
        sort: str = "newest",
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        filters = JobQuery(query=query, location=location, remote=remote, limit=limit)
        clauses, params = self._filters(filters)
        if source:
            clauses.append("j.source = ?")
            params.append(source)
        if min_score is not None:
            clauses.append("COALESCE(s.score, 0) >= ?")
            params.append(min_score)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        order = (
            "ORDER BY (s.score IS NULL) ASC, s.score DESC, j.posted_at DESC"
            if sort == "score"
            else "ORDER BY j.posted_at DESC, j.fetched_at DESC"
        )
        rows = self.db.query(
            f"""SELECT j.*, s.score, s.breakdown, s.matched_skills, s.missing_skills, s.rationale
                FROM jobs j
                LEFT JOIN job_scores s ON s.job_id = j.id AND s.profile_version = ?
                {where} {order} LIMIT ? OFFSET ?""",  # noqa: S608
            (profile_version, *params, limit, offset),
        )
        total_row = self.db.query_one(
            f"""SELECT COUNT(*) AS n FROM jobs j
                LEFT JOIN job_scores s ON s.job_id = j.id AND s.profile_version = ?
                {where}""",  # noqa: S608
            (profile_version, *params),
        )
        items = [self._card_dict(row) for row in rows]
        return {
            "items": items,
            "total": int(total_row["n"]) if total_row else 0,
            "limit": limit,
            "offset": offset,
            "sources": self.source_counts(),
        }

    def detail(self, job_id: str, profile_version: str = "") -> dict[str, Any] | None:
        row = self.db.query_one(
            """SELECT j.*, s.score, s.breakdown, s.matched_skills, s.missing_skills, s.rationale, s.model
               FROM jobs j LEFT JOIN job_scores s ON s.job_id = j.id AND s.profile_version = ?
               WHERE j.id = ?""",
            (profile_version, job_id),
        )
        if not row:
            return None
        job = self._row_to_job(row)
        card = self._card_dict(row)
        card["description"] = job.description
        card["employment_type"] = job.employment_type
        card["tags"] = job.tags
        card["score_detail"] = (
            {
                "score": row["score"],
                "breakdown": json_loads(row["breakdown"], {}),
                "matched_skills": json_loads(row["matched_skills"], []),
                "missing_skills": json_loads(row["missing_skills"], []),
                "rationale": row["rationale"],
                "model": row["model"],
            }
            if row["score"] is not None
            else None
        )
        return card

    def count(self) -> int:
        row = self.db.query_one("SELECT COUNT(*) AS n FROM jobs")
        return int(row["n"]) if row else 0

    def source_counts(self) -> dict[str, int]:
        rows = self.db.query(
            "SELECT source, COUNT(*) AS n FROM jobs GROUP BY source ORDER BY n DESC"
        )
        return {row["source"]: int(row["n"]) for row in rows}

    # ------------------------------------------------------------------ writes
    def upsert(self, jobs: list[Job]) -> list[Job]:
        jobs = dedupe(jobs)
        if not jobs:
            return []
        params = [
            (
                job.id,
                job.source,
                job.external_id,
                job.title,
                job.company,
                job.url,
                job.description,
                job.location,
                None if job.remote is None else int(job.remote),
                job.employment_type,
                job.salary.minimum if job.salary else None,
                job.salary.maximum if job.salary else None,
                job.salary.currency if job.salary else None,
                job.salary.period if job.salary else None,
                to_iso(job.posted_at),
                job.fetched_at,
                job.fingerprint,
                json_dumps(job.tags),
                json_dumps(job.raw),
            )
            for job in jobs
        ]
        self.db.execute_many(
            """INSERT INTO jobs(id, source, external_id, title, company, url, description, location, remote,
               employment_type, salary_min, salary_max, salary_currency, salary_period, posted_at, fetched_at,
               fingerprint, tags, raw)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT DO NOTHING""",
            params,
        )
        # Refresh metadata on a second pass so repeated searches improve old rows.
        updates = []
        for job in jobs:
            salary = job.salary
            has_salary = salary is not None
            updates.append(
                (
                    job.fetched_at,
                    job.description,
                    job.description,
                    has_salary,
                    salary.minimum if salary else None,
                    has_salary,
                    salary.maximum if salary else None,
                    has_salary,
                    salary.currency if salary else None,
                    has_salary,
                    salary.period if salary else None,
                    to_iso(job.posted_at),
                    job.id,
                )
            )
        self.db.execute_many(
            """UPDATE jobs SET fetched_at = ?,
               description = CASE WHEN length(?) > length(description) THEN ? ELSE description END,
               salary_min = CASE WHEN ? THEN ? ELSE salary_min END,
               salary_max = CASE WHEN ? THEN ? ELSE salary_max END,
               salary_currency = CASE WHEN ? THEN ? ELSE salary_currency END,
               salary_period = CASE WHEN ? THEN ? ELSE salary_period END,
               posted_at = COALESCE(posted_at, ?)
               WHERE id = ?""",
            updates,
        )
        stored: list[Job] = []
        for job in jobs:
            existing = self.get(job.id)
            if existing:
                stored.append(existing)
        return stored

    def refresh(self, query: JobQuery) -> dict[str, Any]:
        report: FetchReport = self.registry.fetch_all(query)
        stored = self.upsert(report.jobs)
        self._save_source_health(report)
        return {
            "fetched": report.total_fetched,
            "stored": len(stored),
            "errors": report.errors,
            "counts": report.counts,
            "sources": report.sources_used,
            "duration_ms": report.duration_ms,
        }

    def add_from_url(self, url: str) -> Job:
        raw = self.registry.manual.from_url(url)
        job = to_job(raw)
        self.upsert([job])
        stored = self.get(job.id)
        if stored is None:
            raise RuntimeError("Could not store the job URL.")
        return stored

    def add_from_text(self, text: str, *, title: str = "", company: str = "", url: str = "") -> Job:
        raw = self.registry.manual.from_text(text, title=title, company=company, url=url)
        job = to_job(raw)
        self.upsert([job])
        stored = self.get(job.id)
        if stored is None:
            raise RuntimeError("Could not store the pasted job.")
        return stored

    def _save_source_health(self, report: FetchReport) -> None:
        now = utc_now_iso()
        for name, count in report.counts.items():
            error = report.errors.get(name)
            self.db.execute(
                "INSERT INTO source_health(name, ok, detail, latency_ms, checked_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET ok = excluded.ok, detail = excluded.detail, "
                "latency_ms = excluded.latency_ms, checked_at = excluded.checked_at",
                (name, int(not error), error or f"{count} results", None, now),
            )

    # ----------------------------------------------------------------- helpers
    def _filters(self, query: JobQuery) -> tuple[list[str], list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if query.query.strip():
            fts = self._fts_query(query.query)
            if fts:
                clauses.append("j.rowid IN (SELECT rowid FROM jobs_fts WHERE jobs_fts MATCH ?)")
                params.append(fts)
        if query.location.strip():
            clauses.append("(j.location LIKE ? OR j.remote = 1)")
            params.append(f"%{query.location.strip()}%")
        if query.remote is True:
            clauses.append("(j.remote = 1 OR lower(j.location) LIKE '%remote%')")
        elif query.remote is False:
            clauses.append("(j.remote = 0 OR j.remote IS NULL)")
        if query.posted_within_days:
            clauses.append("j.posted_at >= ?")
            from datetime import UTC, datetime, timedelta

            cutoff = datetime.now(UTC) - timedelta(days=query.posted_within_days)
            params.append(cutoff.date().isoformat())
        return clauses, params

    @staticmethod
    def _fts_query(text: str) -> str:
        tokens = re.findall(r"[A-Za-z0-9+#]{2,}", text)[:8]
        return " OR ".join(f'"{token}"*' for token in tokens)

    @staticmethod
    def _row_to_job(row: Any) -> Job:
        salary = None
        if row["salary_min"] is not None or row["salary_max"] is not None:
            salary = Salary(
                minimum=row["salary_min"],
                maximum=row["salary_max"],
                currency=row["salary_currency"] or "USD",
                period=row["salary_period"] or "year",
            )
        return Job(
            id=row["id"],
            source=row["source"],
            external_id=row["external_id"],
            title=row["title"],
            company=row["company"],
            url=row["url"],
            description=row["description"],
            location=row["location"],
            remote=None if row["remote"] is None else bool(row["remote"]),
            employment_type=row["employment_type"],
            salary=salary,
            posted_at=row["posted_at"],
            fetched_at=row["fetched_at"],
            fingerprint=row["fingerprint"],
            tags=json_loads(row["tags"], []) or [],
            raw=json_loads(row["raw"], {}) or {},
        )

    def _card_dict(self, row: Any) -> dict[str, Any]:
        job = self._row_to_job(row)
        columns = set(row.keys())
        card = self.to_card(job, {"score": row["score"]} if "score" in columns else None)
        payload = card.model_dump()
        payload["salary_label"] = payload.pop("salary")
        payload["matched_skills"] = (
            json_loads(row["matched_skills"], []) if "matched_skills" in columns else []
        )
        payload["missing_skills"] = (
            json_loads(row["missing_skills"], []) if "missing_skills" in columns else []
        )
        payload["rationale"] = row["rationale"] if "rationale" in columns else ""
        payload["breakdown"] = json_loads(row["breakdown"], {}) if "breakdown" in columns else {}
        return payload


def get_jobs() -> JobService:
    # The service captures the active user's database, so it must not be cached
    # across requests in a multi-account process.
    return JobService()


__all__ = ["JobService", "get_jobs"]
