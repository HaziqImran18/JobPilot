"""Job storage, structured search, and refresh orchestration.

Search is deliberately split in two:

* SQL decides cheap, mandatory constraints (source, date, status).
* Python applies the parts that need interpretation: occupation, geography,
  remote eligibility, seniority and employment type.

The second step matters because job boards publish locations as free text and a
``location LIKE '%Toronto%'`` query cannot tell the difference between "remote
job open to Canadians" and "office in Toronto". :mod:`jobpilot.pipelines.locations`
resolves that; this module only applies its verdicts.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
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
from jobpilot.pipelines.categories import (
    category_label,
    classify_category,
    is_related,
    query_terms,
    title_match,
)
from jobpilot.pipelines.extract import dedupe, to_job
from jobpilot.pipelines.intent import SearchIntent, interpret_query
from jobpilot.pipelines.locations import (
    eligibility_label,
    infer_seniority,
    location_match,
    parse_remote_info,
)
from jobpilot.sources.registry import FetchReport, SourceRegistry, get_registry

INACTIVE_STATUSES = ("expired", "unavailable", "removed")
_VERIFIED_LOCATION_TIERS = {
    "exact",
    "region",
    "country",
    "remote_eligible",
    "remote_worldwide",
    "remote",
}
_MAX_CANDIDATES = 4000


def _parse_timestamp(value: Any) -> float:
    if not value:
        return 0.0
    text = str(value).strip()
    if not text:
        return 0.0
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(text[:10])
        except ValueError:
            return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _job_text(job: Job) -> str:
    return normalize_text(f"{job.title}\n{job.description}\n{' '.join(job.tags)}")


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
        location = job.location or job.job_location or ("Remote" if job.remote else "")
        return JobCard(
            id=job.id,
            title=job.title,
            company=job.company,
            location=location,
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
            category=job.category,
            category_label=category_label(job.category),
            seniority=job.seniority,
            employment_type=job.employment_type,
            workplace_type=job.workplace_type,
            job_location=job.job_location,
            remote_scope=job.remote_scope,
            location_confidence=job.location_confidence,
            eligible_locations=job.eligible_locations,
            eligibility_label=eligibility_label(
                job.workplace_type,
                job.remote_scope,
                job.eligible_locations,
                job.job_location or job.location,
            ),
            match_kind=job.match_kind or "exact",
            status=job.status,
        )

    def search(self, query: JobQuery, *, profile_version: str = "") -> list[Job]:
        """Return jobs matching every mandatory filter, best match first."""
        cards = self.list_cards(
            profile_version=profile_version,
            query=query.query,
            location=query.location,
            remote=query.remote,
            posted_within_days=query.posted_within_days,
            category=query.category,
            workplace_type=query.workplace_type,
            seniority=query.seniority,
            employment_type=query.employment_type,
            skills=query.skills,
            include_related=query.include_related,
            sources=query.sources,
            sort="relevance",
            limit=query.limit,
            offset=0,
        )
        jobs: list[Job] = []
        for item in cards["items"]:
            job = self.get(item["id"])
            if job is not None:
                job.match_kind = item.get("match_kind", "exact")
                job.score = item.get("score")
                jobs.append(job)
        return jobs

    def list_cards(
        self,
        *,
        profile_version: str = "",
        query: str = "",
        location: str = "",
        remote: bool | None = None,
        source: str = "",
        sources: list[str] | None = None,
        min_score: float | None = None,
        sort: str = "relevance",
        posted_within_days: int | None = None,
        category: str = "",
        workplace_type: str = "",
        seniority: str = "",
        employment_type: str = "",
        skills: list[str] | None = None,
        include_related: bool = True,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        intent = interpret_query(
            query=query,
            location=location,
            remote=remote,
            category=category,
            seniority=seniority,
            workplace_type=workplace_type,
            employment_type=employment_type,
            posted_within_days=posted_within_days,
            sources=[source] if source else (sources or []),
            include_related=include_related,
        )
        self.backfill_structured()

        source_filter = [source] if source else list(sources or [])
        rows = self._rows(
            sources=source_filter,
            posted_within_days=posted_within_days,
        )
        scores = self._scores(profile_version=profile_version)
        scored_ids = set(scores)

        accepted: list[tuple[Job, str, float, dict[str, Any] | None]] = []
        for row in rows:
            job = self._row_to_job(row)
            ok, match_kind, relevance = self._evaluate(job, intent, skills=skills or [])
            if not ok:
                continue
            score = scores.get(job.id)
            if min_score is not None and (
                score is None or float(score.get("score") or 0) < min_score
            ):
                continue
            accepted.append((job, match_kind, relevance, score))

        accepted.sort(key=lambda item: self._sort_key(item, sort))

        total = len(accepted)
        page = accepted[offset : offset + limit]
        items = []
        for job, match_kind, relevance, score in page:
            job.match_kind = match_kind
            card = self.to_card(job, score).model_dump()
            card["match_kind"] = match_kind
            card["relevance"] = round(relevance * 100, 1)
            if score:
                card["matched_skills"] = json_loads(score.get("matched_skills"), []) or []
                card["missing_skills"] = json_loads(score.get("missing_skills"), []) or []
                card["rationale"] = score.get("rationale") or ""
                card["breakdown"] = json_loads(score.get("breakdown"), {}) or {}
            else:
                card["matched_skills"] = []
                card["missing_skills"] = []
                card["rationale"] = ""
                card["breakdown"] = {}
            items.append(card)

        return {
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
            "exact": sum(1 for item in accepted if item[1] == "exact"),
            "related": sum(1 for item in accepted if item[1] == "related"),
            "sources": self.source_counts(),
            "interpreted": intent.as_dict(),
            "scored": len(scored_ids),
            "retrieved_at": None,
        }

    def detail(self, job_id: str, profile_version: str = "") -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
        if not row:
            return None
        job = self._row_to_job(row)
        score = self._scores(profile_version=profile_version, job_id=job_id).get(job_id)
        card = self.to_card(job, score).model_dump()
        card["salary_label"] = card.pop("salary")
        card["description"] = job.description
        card["employment_type"] = job.employment_type
        card["tags"] = job.tags
        card["company_location"] = job.company_location
        card["first_seen_at"] = job.first_seen_at
        card["last_seen_at"] = job.last_seen_at
        card["score_detail"] = (
            {
                "score": score.get("score"),
                "breakdown": json_loads(score.get("breakdown"), {}),
                "matched_skills": json_loads(score.get("matched_skills"), []),
                "missing_skills": json_loads(score.get("missing_skills"), []),
                "rationale": score.get("rationale") or "",
                "model": score.get("model") or "",
            }
            if score
            else None
        )
        return card

    def count(self) -> int:
        row = self.db.query_one(
            "SELECT COUNT(*) AS n FROM jobs WHERE status NOT IN (?, ?, ?)",
            INACTIVE_STATUSES,
        )
        return int(row["n"]) if row else 0

    def source_counts(self) -> dict[str, int]:
        rows = self.db.query(
            "SELECT source, COUNT(*) AS n FROM jobs WHERE status NOT IN (?, ?, ?) "
            "GROUP BY source ORDER BY n DESC",
            INACTIVE_STATUSES,
        )
        return {row["source"]: int(row["n"]) for row in rows}

    def stats(self) -> dict[str, Any]:
        counts = self.db.query(
            "SELECT source, COUNT(*) AS n FROM jobs WHERE status NOT IN (?, ?, ?) GROUP BY source",
            INACTIVE_STATUSES,
        )
        remote = self.db.query_one(
            "SELECT COUNT(*) AS n FROM jobs WHERE status NOT IN (?, ?, ?) AND workplace_type = 'remote'",
            INACTIVE_STATUSES,
        )
        categories = self.db.query(
            "SELECT category, COUNT(*) AS n FROM jobs WHERE status NOT IN (?, ?, ?) "
            "GROUP BY category ORDER BY n DESC",
            INACTIVE_STATUSES,
        )
        return {
            "total": self.count(),
            "remote": int(remote["n"]) if remote else 0,
            "sources": {row["source"]: int(row["n"]) for row in counts},
            "categories": {
                category_label(row["category"]): int(row["n"]) for row in categories
            },
        }

    # ------------------------------------------------------------------ writes
    @staticmethod
    def _enrich(job: Job) -> Job:
        """Fill structured fields for callers that construct ``Job`` directly."""
        if job.workplace_type and job.category not in {"", "other"}:
            return job
        info = parse_remote_info(
            location=job.location,
            title=job.title,
            description=job.description,
            tags=job.tags,
            raw_remote=job.remote,
        )
        category = job.category
        if not category or category == "other":
            category = classify_category(job.title, job.description, job.tags).category or "other"
        workplace = job.workplace_type or info.workplace_type
        if job.remote is None or job.workplace_type:
            if workplace == "remote" or workplace == "hybrid":
                remote: bool | None = True
            elif workplace == "onsite":
                remote = False
            else:
                remote = job.remote
        else:
            remote = job.remote
        return job.model_copy(
            update={
                "category": category,
                "seniority": job.seniority or infer_seniority(job.title, job.description),
                "workplace_type": workplace,
                "company_location": job.company_location or info.company_location,
                "job_location": job.job_location or info.job_location or job.location,
                "eligible_locations": job.eligible_locations or info.eligible_locations,
                "remote_scope": job.remote_scope
                if job.remote_scope not in {"", "unknown"}
                else info.remote_scope,
                "location_confidence": info.location_confidence,
                "remote": remote,
            }
        )

    def upsert(self, jobs: list[Job]) -> list[Job]:
        jobs = [self._enrich(job) for job in dedupe(jobs)]
        if not jobs:
            return []
        now = utc_now_iso()
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
                job.fetched_at or now,
                job.fingerprint,
                json_dumps(job.tags),
                json_dumps(job.raw),
                job.category or "other",
                job.seniority,
                job.workplace_type,
                job.company_location,
                job.job_location or job.location,
                json_dumps(job.eligible_locations),
                job.remote_scope or "unknown",
                job.location_confidence or "unknown",
                "active",
                job.first_seen_at or job.fetched_at or now,
                job.last_seen_at or now,
                now,
            )
            for job in jobs
        ]
        self.db.execute_many(
            """INSERT INTO jobs(id, source, external_id, title, company, url, description, location,
               remote, employment_type, salary_min, salary_max, salary_currency, salary_period,
               posted_at, fetched_at, fingerprint, tags, raw, category, seniority, workplace_type,
               company_location, job_location, eligible_locations, remote_scope,
               location_confidence, status, first_seen_at, last_seen_at, last_checked_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       ?, ?, ?, ?, ?)
               ON CONFLICT DO NOTHING""",
            params,
        )
        # Refresh metadata on a second pass so repeated searches improve old rows
        # without ever losing the original first-seen timestamp.
        updates = []
        for job in jobs:
            salary = job.salary
            has_salary = salary is not None
            updates.append(
                (
                    job.fetched_at or now,
                    now,
                    now,
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
                    job.category or "other",
                    job.category or "other",
                    job.seniority,
                    job.seniority,
                    job.workplace_type,
                    job.workplace_type,
                    job.company_location,
                    job.company_location,
                    job.job_location or job.location,
                    job.job_location or job.location,
                    json_dumps(job.eligible_locations),
                    json_dumps(job.eligible_locations),
                    job.remote_scope or "unknown",
                    job.remote_scope or "unknown",
                    job.location_confidence or "unknown",
                    job.location_confidence or "unknown",
                    now,
                    job.id,
                )
            )
        self.db.execute_many(
            """UPDATE jobs SET fetched_at = ?, last_seen_at = ?, last_checked_at = ?,
               status = 'active',
               description = CASE WHEN length(?) > length(description) THEN ? ELSE description END,
               salary_min = CASE WHEN ? THEN ? ELSE salary_min END,
               salary_max = CASE WHEN ? THEN ? ELSE salary_max END,
               salary_currency = CASE WHEN ? THEN ? ELSE salary_currency END,
               salary_period = CASE WHEN ? THEN ? ELSE salary_period END,
               posted_at = COALESCE(posted_at, ?),
               remote = CASE WHEN ? IS NULL THEN remote ELSE ? END,
               location = CASE WHEN ? <> '' THEN ? ELSE location END,
               employment_type = CASE WHEN ? <> '' THEN ? ELSE employment_type END,
               category = CASE WHEN ? <> '' THEN ? ELSE category END,
               seniority = CASE WHEN ? <> '' THEN ? ELSE seniority END,
               workplace_type = CASE WHEN ? <> '' THEN ? ELSE workplace_type END,
               company_location = CASE WHEN ? <> '' THEN ? ELSE company_location END,
               job_location = CASE WHEN ? <> '' THEN ? ELSE job_location END,
               eligible_locations = CASE WHEN ? <> '[]' THEN ? ELSE eligible_locations END,
               remote_scope = CASE WHEN ? <> 'unknown' THEN ? ELSE remote_scope END,
               location_confidence = CASE WHEN ? <> 'unknown' THEN ? ELSE location_confidence END,
               first_seen_at = COALESCE(first_seen_at, ?)
               WHERE id = ?""",
            updates,
        )
        stored: list[Job] = []
        for job in jobs:
            existing = self.get(job.id)
            if existing:
                stored.append(existing)
        return stored

    def reclassify_categories(self) -> int:
        """Re-run occupation classification once per account after a fix.

        Stored categories can be wrong after a classifier change. The revision
        marker keeps this a one-off instead of rescanning the board per request.
        """
        marker = self.db.query_one("SELECT value FROM kv WHERE key = 'category_revision'")
        revision = "2"
        if marker is not None and marker["value"] == revision:
            return 0
        rows = self.db.query("SELECT id, title, description, tags FROM jobs")
        updates = []
        for row in rows:
            tags = json_loads(row["tags"], []) or []
            match = classify_category(row["title"] or "", row["description"] or "", tags)
            updates.append((match.category or "other", row["id"]))
        if updates:
            self.db.execute_many("UPDATE jobs SET category = ? WHERE id = ?", updates)
        self.db.execute(
            "INSERT INTO kv(key, value, updated_at) VALUES ('category_revision', ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (revision, utc_now_iso()),
        )
        return len(updates)

    def backfill_structured(self, limit: int = 800) -> int:
        """Give rows stored before structured geography a best-effort upgrade."""
        rows = self.db.query(
            "SELECT id, title, description, location, remote, tags FROM jobs "
            "WHERE workplace_type = '' LIMIT ?",
            (limit,),
        )
        if not rows:
            return 0
        updates = []
        for row in rows:
            tags = json_loads(row["tags"], []) or []
            raw_remote = None if row["remote"] is None else bool(row["remote"])
            info = parse_remote_info(
                location=row["location"] or "",
                title=row["title"] or "",
                description=row["description"] or "",
                tags=tags,
                raw_remote=raw_remote,
            )
            match = classify_category(row["title"] or "", row["description"] or "", tags)
            updates.append(
                (
                    match.category or "other",
                    infer_seniority(row["title"] or "", row["description"] or ""),
                    info.workplace_type,
                    info.company_location,
                    info.job_location or row["location"] or "",
                    json_dumps(info.eligible_locations),
                    info.remote_scope,
                    info.location_confidence,
                    row["id"],
                )
            )
        self.db.execute_many(
            """UPDATE jobs SET category = ?, seniority = ?, workplace_type = ?,
               company_location = ?, job_location = ?, eligible_locations = ?,
               remote_scope = ?, location_confidence = ?
               WHERE id = ? AND workplace_type = ''""",
            updates,
        )
        return len(updates)

    def refresh(self, query: JobQuery) -> dict[str, Any]:
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
        # Adapters need the occupation, not the whole sentence. Keep a usable
        # fallback when the query had no recognised role words.
        retrieval = query.model_copy(
            update={
                "query": intent.search_query or query.query,
                "location": intent.resolved_location.display or query.location,
                "category": intent.category or query.category,
                "workplace_type": intent.workplace_type or query.workplace_type,
            }
        )
        report: FetchReport = self.registry.fetch_all(retrieval)
        stored = self.upsert(report.jobs)
        self._expire_stale()
        self._save_source_health(report)
        return {
            "fetched": report.total_fetched,
            "stored": len(stored),
            "errors": report.errors,
            "counts": report.counts,
            "sources": report.sources_used,
            "source_status": report.source_status(),
            "duration_ms": report.duration_ms,
            "interpreted": intent.as_dict(),
            "retrieved_at": utc_now_iso(),
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

    def _expire_stale(self, days: int = 60) -> None:
        cutoff = (datetime.now(UTC) - timedelta(days=days)).date().isoformat()
        self.db.execute(
            "UPDATE jobs SET status = 'expired' "
            "WHERE status = 'active' AND posted_at IS NOT NULL AND posted_at <> '' AND posted_at < ?",
            (cutoff,),
        )

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
    def _rows(
        self,
        *,
        sources: list[str] | None = None,
        posted_within_days: int | None = None,
    ) -> list[Any]:
        clauses = ["j.status NOT IN (?, ?, ?)"]
        params: list[Any] = list(INACTIVE_STATUSES)
        if sources:
            placeholders = ", ".join("?" for _ in sources)
            clauses.append(f"j.source IN ({placeholders})")
            params.extend(sources)
        if posted_within_days:
            cutoff = (datetime.now(UTC) - timedelta(days=posted_within_days)).date().isoformat()
            clauses.append("COALESCE(j.posted_at, j.first_seen_at) >= ?")
            params.append(cutoff)
        where = " AND ".join(clauses)
        return self.db.query(
            f"SELECT j.* FROM jobs j WHERE {where} "  # noqa: S608 - clauses are fixed strings
            "ORDER BY COALESCE(j.posted_at, j.first_seen_at) DESC, j.fetched_at DESC LIMIT ?",
            (*params, _MAX_CANDIDATES),
        )

    def _scores(self, *, profile_version: str, job_id: str = "") -> dict[str, dict[str, Any]]:
        if not profile_version:
            return {}
        if job_id:
            rows = self.db.query(
                "SELECT * FROM job_scores WHERE profile_version = ? AND job_id = ?",
                (profile_version, job_id),
            )
        else:
            rows = self.db.query(
                "SELECT * FROM job_scores WHERE profile_version = ?", (profile_version,)
            )
        return {row["job_id"]: dict(row) for row in rows}

    def _evaluate(
        self,
        job: Job,
        intent: SearchIntent,
        *,
        skills: list[str],
    ) -> tuple[bool, str, float]:
        """Return ``(accepted, match_kind, relevance)`` for one posting."""
        request = intent.resolved_location
        if request.known or request.remote_only:
            tier, _note = location_match(
                request,
                job_location=job.job_location or job.location,
                workplace_type=job.workplace_type,
                remote_scope=job.remote_scope,
                eligible_locations=job.eligible_locations,
            )
            if request.city:
                allowed = {"exact", "remote_eligible", "remote_worldwide", "remote"}
            elif request.region:
                allowed = {
                    "exact",
                    "region",
                    "remote_eligible",
                    "remote_worldwide",
                    "remote",
                }
            else:
                allowed = _VERIFIED_LOCATION_TIERS
            if tier not in allowed:
                return False, "", 0.0

        if intent.workplace_type:
            if intent.workplace_type == "remote":
                if job.workplace_type != "remote":
                    return False, "", 0.0
            elif job.workplace_type != intent.workplace_type:
                return False, "", 0.0

        if intent.seniority and job.seniority != intent.seniority:
            return False, "", 0.0
        if intent.employment_type and job.employment_type != intent.employment_type:
            return False, "", 0.0
        if skills:
            have = {normalize_text(value) for value in job.tags}
            have.update(query_terms(f"{job.title} {job.description}"))
            missing = [skill for skill in skills if normalize_text(skill) not in have]
            if missing:
                return False, "", 0.0

        match_kind = "exact"
        relevance = 1.0
        if intent.category:
            if job.category == intent.category:
                match_kind = "exact"
                relevance = max(
                    0.55,
                    title_match(job.title, intent.search_query or category_label(job.category)),
                )
            elif intent.include_related and is_related(job.category, intent.category):
                if not self._related_allowed(job, intent):
                    return False, "", 0.0
                match_kind = "related"
                overlap = self._overlap(job, intent.search_query)
                relevance = max(0.3, 0.6 * overlap)
            else:
                return False, "", 0.0
        elif intent.search_query:
            overlap = self._overlap(job, intent.search_query)
            title_score = title_match(job.title, intent.search_query)
            # Require every meaningful term unless the title itself is a full
            # match. Substring matching would let "ai" match "maintain".
            if overlap < 1.0 and title_score < 1.0:
                return False, "", 0.0
            relevance = max(overlap, title_score)
        return True, match_kind, min(1.0, relevance)

    def _related_allowed(self, job: Job, intent: SearchIntent) -> bool:
        """Decide whether a same-family occupation may appear as a related hit.

        A related role is only useful when it is genuinely close to what was
        asked for. Two rules keep that honest:

        * A broad query (one meaningful term or none) may show related roles.
        * A specific query may show a related role only when the posting itself
          mentions every term of that query.
        """
        terms = query_terms(intent.search_query)
        if len(terms) <= 1:
            return True
        return self._overlap(job, intent.search_query) >= 1.0

    @staticmethod
    def _overlap(job: Job, text: str) -> float:
        terms = query_terms(text)
        if not terms:
            return 1.0
        haystack = _job_text(job)
        hits = sum(
            1
            for term in terms
            if re.search(
                rf"(?<![a-z0-9]){re.escape(normalize_text(term))}(?![a-z0-9])",
                haystack,
            )
        )
        return hits / len(terms)

    @staticmethod
    def _sort_key(
        item: tuple[Job, str, float, dict[str, Any] | None],
        sort: str,
    ) -> tuple[Any, ...]:
        job, match_kind, relevance, score = item
        score_value = float(score.get("score") or 0) if score else 0.0
        freshness = _parse_timestamp(job.posted_at or job.first_seen_at or job.last_seen_at)
        salary_value = 0.0
        if job.salary:
            salary_value = float(job.salary.maximum or job.salary.minimum or 0)
        kind_rank = 0 if match_kind == "exact" else 1
        if sort == "oldest":
            return (freshness or float("inf"), job.title)
        if sort == "score":
            return (score_value == 0, -score_value, -freshness, job.title)
        if sort == "salary":
            return (salary_value == 0, -salary_value, -freshness, job.title)
        if sort == "newest":
            return (-freshness, -score_value, job.title)
        # Default: honour the ranking contract — occupation/geography first,
        # profile fit next, freshness last.
        return (kind_rank, -relevance, -score_value, -freshness, job.title)

    @staticmethod
    def _fts_query(text: str) -> str:
        """Kept for callers/tests that inspect the legacy full-text query.

        Candidate retrieval now happens in SQL and relevance filtering happens in
        Python, but the tokenizer rule is unchanged: every meaningful word must
        appear, so "AI engineer" does not return every job mentioning either.
        """
        tokens = re.findall(r"[A-Za-z0-9+#]{2,}", text)[:8]
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
            category=row["category"] or "other",
            seniority=row["seniority"] or "",
            workplace_type=row["workplace_type"] or "",
            company_location=row["company_location"] or "",
            job_location=row["job_location"] or row["location"] or "",
            eligible_locations=json_loads(row["eligible_locations"], []) or [],
            remote_scope=row["remote_scope"] or "unknown",
            location_confidence=row["location_confidence"] or "unknown",
            status=row["status"] or "active",
            first_seen_at=row["first_seen_at"],
            last_seen_at=row["last_seen_at"],
            last_checked_at=row["last_checked_at"],
        )


def get_jobs() -> JobService:
    # The service captures the active user's database, so it must not be cached
    # across requests in a multi-account process.
    return JobService()


__all__ = ["JobService", "get_jobs"]
