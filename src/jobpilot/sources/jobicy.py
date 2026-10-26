"""Jobicy adapter — a free remote-jobs API that needs no key."""

from __future__ import annotations

from typing import Any

from jobpilot.core.models import JobQuery, RawJob, Salary
from jobpilot.core.utils import html_to_text, parse_datetime
from jobpilot.sources.base import SourceAdapter, SourceError

API_URL = "https://jobicy.com/api/v2/remote-jobs"


class JobicyAdapter(SourceAdapter):
    name = "jobicy"
    kind = "api"
    priority = 4
    _min_interval = 1.0

    def enabled(self) -> bool:
        return self.settings.enable_jobicy

    def _probe(self) -> None:
        data = self.get_json(API_URL, {"count": 1})
        if not isinstance(data, dict) or "jobs" not in data:
            raise SourceError("Unexpected Jobicy response")

    def fetch(self, query: JobQuery) -> list[RawJob]:
        if query.remote is False:
            return []
        params: dict[str, Any] = {"count": min(max(query.limit * 2, 10), 50)}
        if query.query:
            params["tag"] = query.query
        data = self.get_json(API_URL, params)
        jobs: list[RawJob] = []
        for item in data.get("jobs", []):
            location = item.get("jobGeo") or "Remote"
            if query.location and not _matches_location(query.location, location):
                continue
            salary = _salary(item)
            jobs.append(
                RawJob(
                    source=self.name,
                    external_id=str(item.get("id") or item.get("jobSlug", "")),
                    title=item.get("jobTitle", ""),
                    company=item.get("companyName", ""),
                    url=item.get("url", ""),
                    description=html_to_text(item.get("jobDescription", "")),
                    location=location,
                    remote=True,
                    employment_type=", ".join(item.get("jobType") or []),
                    salary=salary,
                    posted_at=parse_datetime(item.get("pubDate")),
                    tags=[
                        tag
                        for tag in [*(item.get("jobIndustry") or []), item.get("jobLevel")]
                        if tag
                    ],
                    raw={"level": item.get("jobLevel"), "geo": item.get("jobGeo")},
                )
            )
            if len(jobs) >= query.limit * 2:
                break
        return jobs


def _salary(item: dict[str, Any]) -> Salary | None:
    minimum = _number(item.get("salaryMin") or item.get("annualSalaryMin"))
    maximum = _number(item.get("salaryMax") or item.get("annualSalaryMax"))
    if minimum is None and maximum is None:
        return None
    return Salary(
        minimum=minimum,
        maximum=maximum,
        currency=item.get("salaryCurrency") or "USD",
        period=_period(item.get("salaryPeriod")),
    )


def _period(value: Any) -> str:
    normalized = str(value or "year").strip().lower()
    if normalized in {"yearly", "annual", "annually", "per year"}:
        return "year"
    if normalized in {"monthly", "per month"}:
        return "month"
    if normalized in {"daily", "per day"}:
        return "day"
    if normalized in {"hourly", "per hour"}:
        return "hour"
    if normalized in {"weekly", "per week"}:
        return "week"
    return normalized


def _number(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _matches_location(requested: str, location: str) -> bool:
    wanted = requested.split(",")[0].strip().lower()
    return not wanted or wanted in location.lower() or "remote" in location.lower() or "anywhere" in location.lower()
