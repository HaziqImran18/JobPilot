"""LinkedIn adapter — public guest job search, no account or cookie required.

LinkedIn exposes the same cards that a logged-out visitor sees on its public
job search. JobPilot reads only those public pages, keeps a short delay between
requests, and never signs in, applies, or messages anyone.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from jobpilot.core.models import JobQuery, RawJob
from jobpilot.core.utils import parse_datetime, parse_relative_date
from jobpilot.pipelines.categories import category_label
from jobpilot.pipelines.extract import infer_employment_type, parse_salary
from jobpilot.sources.base import SourceAdapter, SourceError, looks_remote

BASE_URL = "https://www.linkedin.com"
SEARCH_URL = f"{BASE_URL}/jobs-guest/jobs/api/seeMoreJobPostings/search"
DETAIL_URL = f"{BASE_URL}/jobs-guest/jobs/api/jobPosting"
PAGE_SIZE = 10
MAX_PAGES = 3


class LinkedInAdapter(SourceAdapter):
    name = "linkedin"
    kind = "html"
    priority = 5
    _min_interval = 0.6

    def enabled(self) -> bool:
        return self.settings.enable_linkedin

    def _probe(self) -> None:
        html = self.get_html(SEARCH_URL, _search_params("software engineer", "Pakistan", 0))
        if not parse_linkedin_cards(html):
            raise SourceError("Unexpected LinkedIn guest-search response")

    def fetch(self, query: JobQuery) -> list[RawJob]:
        keywords = (
            query.query.strip()
            or category_label(query.category)
            or "jobs"
        )
        location = query.location.strip()
        remote_only = query.workplace_type == "remote" or query.remote is True
        if not location:
            location = "Worldwide"
        wanted = max(query.limit * 2, 20)
        jobs: list[RawJob] = []
        seen: set[str] = set()
        for page in range(MAX_PAGES):
            start = page * PAGE_SIZE
            if start >= wanted:
                break
            params = _search_params(
                keywords,
                location,
                start,
                query.posted_within_days,
                remote=remote_only,
            )
            html = self.get_html(SEARCH_URL, params)
            cards = parse_linkedin_cards(html)
            if not cards:
                break
            for job in cards:
                if job.external_id in seen:
                    continue
                seen.add(job.external_id)
                jobs.append(job)
            if len(cards) < PAGE_SIZE:
                break
        if not jobs:
            return []
        # Guest search cards have no description. Read a few detail pages so
        # matching has real evidence to work with.
        for job in jobs[: min(len(jobs), 4)]:
            try:
                detail = self.get_html(f"{DETAIL_URL}/{job.external_id}")
            except SourceError:
                continue
            parsed = parse_linkedin_detail(detail)
            if parsed["description"]:
                job.description = parsed["description"]
            job.salary = job.salary or parse_salary(parsed["description"])
            job.posted_at = job.posted_at or parsed["posted_at"]
            if parsed["employment_type"]:
                job.employment_type = parsed["employment_type"]
        return jobs[:wanted]


def _search_params(
    keywords: str,
    location: str,
    start: int,
    posted_within_days: int | None = None,
    remote: bool = False,
) -> dict[str, str | int]:
    params: dict[str, str | int] = {
        "keywords": keywords,
        "location": location,
        "start": start,
    }
    if posted_within_days:
        params["f_TPR"] = f"r{posted_within_days * 24 * 60 * 60}"
    if remote:
        # LinkedIn's public guest UI uses f_WT=2 for remote roles.
        params["f_WT"] = "2"
    return params


def parse_linkedin_cards(html: str) -> list[RawJob]:
    soup = BeautifulSoup(html, "html.parser")
    jobs: list[RawJob] = []
    for card in soup.select("li .base-search-card, li .job-search-card"):
        link = card.select_one("a.base-card__full-link") or card.find(
            "a", href=re.compile(r"/jobs/view/")
        )
        if link is None:
            continue
        href = link.get("href", "")
        external_id = _job_id(card, href)
        if not external_id:
            continue
        title = _text(card.select_one(".base-search-card__title, h3")) or _text(
            card.select_one("span.sr-only")
        )
        company = _text(card.select_one(".base-search-card__subtitle, h4")) or "Unknown company"
        location = _text(card.select_one(".job-search-card__location"))
        time_node = card.select_one("time")
        posted = parse_datetime(time_node.get("datetime")) if time_node else None
        if posted is None:
            posted = parse_relative_date(_text(time_node) if time_node else "")
        jobs.append(
            RawJob(
                source="linkedin",
                external_id=external_id,
                title=title,
                company=company,
                url=urljoin(BASE_URL, href.split("?")[0]),
                description="",
                location=location,
                remote=True if looks_remote(f"{title} {location}") else None,
                employment_type=infer_employment_type(title),
                posted_at=posted,
                tags=[],
                raw={"guest": True, "urn": card.get("data-entity-urn", "")},
            )
        )
    return jobs


def parse_linkedin_detail(html: str) -> dict[str, object]:
    soup = BeautifulSoup(html, "html.parser")
    description_node = soup.select_one(
        ".show-more-less-html__markup, .description__text, "
        "[class*='show-more-less-html__markup']"
    )
    description = _text(description_node, separator="\n")
    posted_node = soup.select_one(
        ".posted-time-ago__text, .job-details-jobs-unified-top-card__posted-date, time"
    )
    posted_at = parse_relative_date(_text(posted_node))
    if posted_at is None and posted_node is not None and posted_node.get("datetime"):
        posted_at = parse_datetime(posted_node.get("datetime"))
    salary_node = soup.select_one(
        ".salary, .compensation__salary, [class*='compensation__salary']"
    )
    salary = parse_salary(_text(salary_node)) if salary_node is not None else None
    return {
        "description": description,
        "posted_at": posted_at,
        "employment_type": infer_employment_type(description),
        "salary": salary or parse_salary(description),
    }


def _job_id(card, href: str) -> str:
    urn = card.get("data-entity-urn", "") if card is not None else ""
    match = re.search(r"jobPosting:(\d+)", urn)
    if match:
        return match.group(1)
    match = re.search(r"/jobs/view/(?:[^/?#]+-)?(\d+)", href)
    return match.group(1) if match else ""


def _text(node, separator: str = " ") -> str:
    if node is None:
        return ""
    return re.sub(r"\s+", " ", node.get_text(separator, strip=True)).strip()


__all__ = ["LinkedInAdapter", "parse_linkedin_cards", "parse_linkedin_detail"]
