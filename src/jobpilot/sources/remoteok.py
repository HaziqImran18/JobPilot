"""RemoteOK adapter — free public JSON feed of remote jobs."""

from __future__ import annotations

from typing import Any

from jobpilot.core.models import JobQuery, RawJob, Salary
from jobpilot.core.utils import parse_datetime
from jobpilot.sources.base import SourceAdapter, SourceError

API_URL = "https://remoteok.com/api"


class RemoteOKAdapter(SourceAdapter):
    name = "remoteok"
    kind = "api"
    priority = 3
    _min_interval = 1.0

    def enabled(self) -> bool:
        return self.settings.enable_remoteok

    def _probe(self) -> None:
        data = self.get_json(API_URL)
        if not isinstance(data, list):
            raise SourceError("Unexpected RemoteOK response")

    def fetch(self, query: JobQuery) -> list[RawJob]:
        if query.remote is False:
            return []
        data = self.get_json(API_URL)
        jobs: list[RawJob] = []
        terms = [term.lower() for term in query.query.split() if len(term) > 2]
        for item in data:
            if not isinstance(item, dict) or "position" not in item:
                continue
            haystack = f"{item.get('position', '')} {item.get('description', '')} {' '.join(item.get('tags') or [])}".lower()
            if terms and not any(term in haystack for term in terms):
                continue
            salary = None
            if item.get("salary_min") or item.get("salary_max"):
                salary = Salary(
                    minimum=_float_or_none(item.get("salary_min")),
                    maximum=_float_or_none(item.get("salary_max")),
                    currency="USD",
                    period="year",
                )
            jobs.append(
                RawJob(
                    source=self.name,
                    external_id=str(item.get("id") or item.get("slug", "")),
                    title=item.get("position", ""),
                    company=item.get("company", ""),
                    url=item.get("url") or item.get("apply_url", ""),
                    description=item.get("description", ""),
                    location=item.get("location") or "Remote",
                    remote=True,
                    salary=salary,
                    posted_at=parse_datetime(item.get("date")),
                    tags=list(item.get("tags") or []),
                    raw={"slug": item.get("slug"), "id": item.get("id")},
                )
            )
            if len(jobs) >= query.limit * 2:
                break
        return jobs


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None
