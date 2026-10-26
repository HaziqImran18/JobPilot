"""Arbeitnow adapter — free European job board API with remote roles."""

from __future__ import annotations

from jobpilot.core.models import JobQuery, RawJob
from jobpilot.core.utils import parse_datetime
from jobpilot.sources.base import SourceAdapter, SourceError

API_URL = "https://www.arbeitnow.com/api/job-board-api"


class ArbeitnowAdapter(SourceAdapter):
    name = "arbeitnow"
    kind = "api"
    priority = 2
    _min_interval = 1.0

    def enabled(self) -> bool:
        return self.settings.enable_arbeitnow

    def _probe(self) -> None:
        data = self.get_json(API_URL)
        if not isinstance(data, dict) or "data" not in data:
            raise SourceError("Unexpected Arbeitnow response")

    def fetch(self, query: JobQuery) -> list[RawJob]:
        data = self.get_json(API_URL)
        jobs: list[RawJob] = []
        terms = [term.lower() for term in query.query.split() if len(term) > 2]
        for item in data.get("data", []):
            haystack = f"{item.get('title', '')} {item.get('description', '')} {' '.join(item.get('tags') or [])}".lower()
            if terms and not any(term in haystack for term in terms):
                continue
            location = item.get("location") or ("Remote" if item.get("remote") else "")
            jobs.append(
                RawJob(
                    source=self.name,
                    external_id=item.get("slug") or item.get("url", ""),
                    title=item.get("title", ""),
                    company=item.get("company_name", ""),
                    url=item.get("url", ""),
                    description=item.get("description", ""),
                    location=location,
                    remote=bool(item.get("remote")),
                    employment_type=", ".join(item.get("job_types") or []),
                    posted_at=parse_datetime(item.get("created_at")),
                    tags=list(item.get("tags") or []),
                    raw={"slug": item.get("slug")},
                )
            )
            if len(jobs) >= query.limit * 2:
                break
        return jobs
