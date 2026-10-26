"""Remotive adapter — a free remote-jobs API that needs no key."""

from __future__ import annotations

from typing import Any

from jobpilot.core.models import JobQuery, RawJob
from jobpilot.core.utils import parse_datetime
from jobpilot.pipelines.extract import parse_salary
from jobpilot.sources.base import SourceAdapter, SourceError

API_URL = "https://remotive.com/api/remote-jobs"


class RemotiveAdapter(SourceAdapter):
    name = "remotive"
    kind = "api"
    priority = 1
    _min_interval = 1.0

    def enabled(self) -> bool:
        return self.settings.enable_remotive

    def _probe(self) -> None:
        data = self.get_json(API_URL, {"limit": 1})
        if not isinstance(data, dict) or "jobs" not in data:
            raise SourceError("Unexpected Remotive response")

    def fetch(self, query: JobQuery) -> list[RawJob]:
        if query.remote is False:
            return []  # Remotive only lists remote roles
        params: dict[str, Any] = {"limit": min(max(query.limit, 1), 100)}
        if query.query:
            params["search"] = query.query
        data = self.get_json(API_URL, params)
        jobs: list[RawJob] = []
        for item in data.get("jobs", []):
            location = item.get("candidate_required_location") or "Remote"
            if (
                query.location
                and not query.remote
                and query.location.split(",")[0].lower() not in location.lower()
            ):
                continue
            jobs.append(
                RawJob(
                    source=self.name,
                    external_id=str(item.get("id", item.get("url", ""))),
                    title=item.get("title", ""),
                    company=item.get("company_name", ""),
                    url=item.get("url", ""),
                    description=item.get("description", ""),
                    location=location,
                    remote=True,
                    employment_type=item.get("job_type", ""),
                    salary=parse_salary(item.get("salary", "")),
                    posted_at=parse_datetime(item.get("publication_date")),
                    tags=[tag for tag in [item.get("category"), *(item.get("tags") or [])] if tag],
                    raw={"id": item.get("id"), "category": item.get("category")},
                )
            )
        return jobs
