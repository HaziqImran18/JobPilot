"""Himalayas adapter — a free, keyless remote-job feed."""

from __future__ import annotations

from typing import Any

from jobpilot.core.models import JobQuery, RawJob, Salary
from jobpilot.core.utils import html_to_text, parse_datetime
from jobpilot.sources.base import SourceAdapter, SourceError

API_URL = "https://himalayas.app/jobs/api"


class HimalayasAdapter(SourceAdapter):
    name = "himalayas"
    kind = "api"
    priority = 5
    _min_interval = 1.0

    def enabled(self) -> bool:
        return self.settings.enable_himalayas

    def _probe(self) -> None:
        data = self.get_json(API_URL, {"limit": 1})
        if not isinstance(data, dict) or "jobs" not in data:
            raise SourceError("Unexpected Himalayas response")

    def fetch(self, query: JobQuery) -> list[RawJob]:
        if query.remote is False:
            return []
        # The feed has no server-side search. Fetch a page and filter locally so
        # we keep requests light and predictable.
        data = self.get_json(API_URL, {"limit": 100})
        terms = [term.lower() for term in query.query.split() if len(term) > 2]
        jobs: list[RawJob] = []
        for item in data.get("jobs", []):
            title = item.get("title", "")
            description = html_to_text(item.get("description", ""))
            haystack = f"{title} {' '.join(item.get('categories') or [])} {description}".lower()
            if terms and not any(term in haystack for term in terms):
                continue
            location = _location(item)
            if query.location and not _matches_location(query.location, location):
                continue
            jobs.append(
                RawJob(
                    source=self.name,
                    external_id=item.get("guid") or item.get("applicationLink", ""),
                    title=title,
                    company=item.get("companyName", ""),
                    url=item.get("applicationLink") or item.get("guid", ""),
                    description=description,
                    location=location,
                    remote=True,
                    employment_type=item.get("employmentType", ""),
                    salary=_salary(item),
                    posted_at=parse_datetime(item.get("pubDate")),
                    tags=[
                        tag.replace("-", " ")
                        for tag in [
                            *(item.get("categories") or [])[:6],
                            *(item.get("seniority") or []),
                        ]
                    ],
                    raw={"guid": item.get("guid")},
                )
            )
            if len(jobs) >= query.limit * 2:
                break
        return jobs


def _location(item: dict[str, Any]) -> str:
    restrictions = [value for value in (item.get("locationRestrictions") or []) if value]
    return ", ".join(restrictions) if restrictions else "Remote"


def _salary(item: dict[str, Any]) -> Salary | None:
    minimum = _number(item.get("minSalary"))
    maximum = _number(item.get("maxSalary"))
    if minimum is None and maximum is None:
        return None
    period = item.get("salaryPeriod") or "year"
    return Salary(
        minimum=minimum,
        maximum=maximum,
        currency=item.get("currency") or "USD",
        period="year" if period == "annual" else period,
    )


def _number(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _matches_location(requested: str, location: str) -> bool:
    wanted = requested.split(",")[0].strip().lower()
    return not wanted or wanted in location.lower() or "remote" in location.lower() or "anywhere" in location.lower()
