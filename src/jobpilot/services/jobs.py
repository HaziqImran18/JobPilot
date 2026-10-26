"""Job storage, search and refresh orchestration."""

from __future__ import annotations

import re
from typing import Any

from jobpilot.core.db import Database, get_db
from jobpilot.core.models import Job, JobCard, JobQuery, Salary
from jobpilot.core.utils import (
    json_dumps,
    json_loads,
    money_label,
    normalize_text,
    to_iso,
    utc_now_iso,
)
from jobpilot.pipelines.extract import dedupe, to_job
from jobpilot.sources.registry import FetchReport, SourceRegistry, get_registry

_PAKISTAN_CITIES = (
    "pakistan",
    "lahore",
    "karachi",
    "islamabad",
    "rawalpindi",
    "faisalabad",
    "multan",
    "peshawar",
    "quetta",
    "hyderabad",
    "gujranwala",
    "sialkot",
    "bahawalpur",
    "sargodha",
    "abbottabad",
    "sukkur",
    "larkana",
    "jhelum",
    "okara",
    "sahiwal",
    "wah cantt",
    "taxila",
    "mardan",
    "nowshera",
    "charsadda",
    "dera ghazi khan",
    "dera ismail khan",
    "rahim yar khan",
    "jhang",
    "sheikhupura",
    "kasur",
    "gujrat",
    "mandi bahauddin",
    "chiniot",
    "muzaffarabad",
    "muzaffargarh",
    "kotli",
    "attock",
    "hafizabad",
    "mirpur",
    "bannu",
    "kohat",
    "swabi",
    "gilgit",
    "skardu",
    "ajk",
    "azad kashmir",
)

# Location filters are deliberately explicit. A location search must never be
# satisfied by a remote job whose location never mentions that place.
_LOCATION_GROUPS: dict[str, tuple[str, ...]] = {
    "pakistan": _PAKISTAN_CITIES,
    "pk": _PAKISTAN_CITIES,
    "punjab": (
        "punjab",
        "lahore",
        "rawalpindi",
        "faisalabad",
        "multan",
        "gujranwala",
        "sialkot",
        "bahawalpur",
        "sargodha",
        "jhelum",
        "okara",
        "sahiwal",
        "jhang",
        "sheikhupura",
        "kasur",
        "gujrat",
        "attock",
        "hafizabad",
    ),
    "sindh": ("sindh", "karachi", "hyderabad", "sukkur", "larkana", "khairpur"),
    "khyber pakhtunkhwa": (
        "khyber pakhtunkhwa",
        "kpk",
        "kp",
        "peshawar",
        "abbottabad",
        "mardan",
        "nowshera",
        "charsadda",
        "dera ismail khan",
        "bannu",
        "kohat",
        "swabi",
    ),
    "kpk": (
        "khyber pakhtunkhwa",
        "kpk",
        "kp",
        "peshawar",
        "abbottabad",
        "mardan",
        "nowshera",
        "charsadda",
        "dera ismail khan",
        "bannu",
        "kohat",
        "swabi",
    ),
    "balochistan": ("balochistan", "quetta", "gwadar", "turbat", "khuzdar"),
    "islamabad": ("islamabad", "islamabad capital territory", "ict"),
    "rawalpindi": ("rawalpindi", "pindi"),
    "karachi": ("karachi", "karāchi"),
    "lahore": ("lahore", "lāhore"),
}
_REMOTE_LOCATION_TERMS = (
    "remote",
    "anywhere",
    "worldwide",
    "work from home",
    "wfh",
    "distributed",
    "global",
)


def _is_remote_location(location: str) -> bool:
    return any(term in location for term in _REMOTE_LOCATION_TERMS)


def _location_terms(location: str) -> tuple[str, ...]:
    """Expand a city or country into the spellings boards actually publish."""
    cleaned = re.sub(r"\s*,\s*", ",", location).strip()
    parts = [part.strip() for part in cleaned.split(",") if part.strip()]
    primary = parts[0] if parts else cleaned
    terms: list[str] = list(_LOCATION_GROUPS.get(primary, (primary,)))
    # Keep the original phrase as well ("islamabad, pakistan") for exact rows.
    if len(parts) > 1:
        terms.append(cleaned)
    seen: set[str] = set()
    ordered: list[str] = []
    for term in terms:
        if term and term not in seen:
            seen.add(term)
            ordered.append(term)
    return tuple(ordered)


def _remote_clause() -> str:
    parts = ["j.remote = 1"]
    parts.extend(
        f"lower(COALESCE(j.location, '')) LIKE '%{term}%'" for term in _REMOTE_LOCATION_TERMS
    )
    return f"({' OR '.join(parts)})"


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
        posted_within_days: int | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        filters = JobQuery(
            query=query,
            location=location,
            remote=remote,
            limit=limit,
            posted_within_days=posted_within_days,
        )
        clauses, params = self._filters(filters)
        if source:
            clauses.append("j.source = ?")
            params.append(source)
        if min_score is not None:
            clauses.append("COALESCE(s.score, 0) >= ?")
            params.append(min_score)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        if sort == "score":
            order = "ORDER BY (s.score IS NULL) ASC, s.score DESC, j.posted_at DESC"
        elif sort == "salary":
            order = (
                "ORDER BY (COALESCE(j.salary_max, j.salary_min) IS NULL) ASC, "
                "COALESCE(j.salary_max, j.salary_min) DESC, j.posted_at DESC"
            )
        elif sort == "oldest":
            order = "ORDER BY j.posted_at ASC, j.fetched_at ASC"
        else:
            order = "ORDER BY j.posted_at DESC, j.fetched_at DESC"
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
                    None if job.remote is None else int(job.remote),
                    None if job.remote is None else int(job.remote),
                    job.location,
                    job.location,
                    job.employment_type,
                    job.employment_type,
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
               posted_at = COALESCE(posted_at, ?),
               remote = CASE WHEN ? IS NULL THEN remote ELSE ? END,
               location = CASE WHEN ? <> '' THEN ? ELSE location END,
               employment_type = CASE WHEN ? <> '' THEN ? ELSE employment_type END
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
        location = normalize_text(query.location)
        if location:
            if _is_remote_location(location):
                clauses.append(_remote_clause())
            else:
                terms = _location_terms(location)
                matches = " OR ".join("lower(j.location) LIKE ?" for _ in terms)
                clauses.append(f"({matches})")
                params.extend(f"%{term}%" for term in terms)
        if query.remote is True:
            clauses.append(_remote_clause())
        elif query.remote is False:
            non_remote = ["COALESCE(j.remote, 0) = 0"]
            non_remote.extend(
                f"lower(COALESCE(j.location, '')) NOT LIKE '%{term}%'"
                for term in _REMOTE_LOCATION_TERMS
            )
            clauses.append(f"({' AND '.join(non_remote)})")
        if query.sources:
            placeholders = ", ".join("?" for _ in query.sources)
            clauses.append(f"j.source IN ({placeholders})")
            params.extend(query.sources)
        if query.posted_within_days:
            from datetime import UTC, datetime, timedelta

            cutoff = datetime.now(UTC) - timedelta(days=query.posted_within_days)
            clauses.append("j.posted_at >= ?")
            params.append(cutoff.date().isoformat())
        return clauses, params

    @staticmethod
    def _fts_query(text: str) -> str:
        tokens = re.findall(r"[A-Za-z0-9+#]{2,}", text)[:8]
        # Every word has to appear somewhere in the posting. Using OR here made
        # a search for "AI engineer" return every job mentioning either word.
        return " AND ".join(f'"{token}"*' for token in tokens)

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
