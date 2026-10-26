"""Talent.com adapter — Pakistan-focused aggregator with public HTML pages."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup

from jobpilot.core.models import JobQuery, RawJob
from jobpilot.core.utils import parse_relative_date
from jobpilot.pipelines.extract import infer_employment_type, parse_salary
from jobpilot.sources.base import SourceAdapter, SourceError, looks_remote

BASE_URL = "https://pk.talent.com"
SEARCH_URL = f"{BASE_URL}/jobs"


class TalentAdapter(SourceAdapter):
    name = "talent"
    kind = "html"
    priority = 7
    _min_interval = 0.3

    def enabled(self) -> bool:
        return self.settings.enable_talent

    def _probe(self) -> None:
        html = self.get_html(SEARCH_URL, {"k": "software engineer", "l": "Pakistan"})
        if not parse_talent_cards(html):
            raise SourceError("Unexpected Talent.com page layout")

    def fetch(self, query: JobQuery) -> list[RawJob]:
        params = {
            "k": query.query.strip() or "software engineer",
            "l": query.location.strip() or "Pakistan",
        }
        html = self.get_html(SEARCH_URL, params)
        jobs = parse_talent_cards(html)
        if not jobs:
            return []
        limit = max(query.limit * 2, 20)
        jobs = jobs[:limit]
        # Listing snippets are short. Read the first few detail pages so the
        # matcher sees the real requirements, not just the teaser.
        for job in jobs[: min(len(jobs), 10)]:
            try:
                detail = self.get_html(job.url)
            except SourceError:
                continue
            description = parse_talent_detail(detail)
            if description and len(description) > len(job.description):
                job.description = description
                job.salary = job.salary or parse_talent_salary(description[:6000])
                job.posted_at = parse_relative_date(description[:400]) or job.posted_at
        return jobs


def parse_talent_cards(html: str) -> list[RawJob]:
    soup = BeautifulSoup(html, "html.parser")
    jobs: list[RawJob] = []
    for card in soup.select(".JobCard_card__TSiPB, article[class*='JobCard_card']"):
        link = card.select_one("a[href*='/view?id=']")
        if link is None:
            link = card.find("a", href=True)
        if link is None:
            continue
        href = link.get("href", "")
        external_id = _view_id(href)
        if not external_id:
            continue
        title = _text(card.select_one(".JobCard_title__X32Qk, h2, h3")) or _text(link)
        company = _text(card.select_one(".JobCard_company__NmRol")) or "Unknown company"
        location = _text(card.select_one(".JobCard_location__nmTtw"))
        body = _text(card.select_one(".JobCard_body__Q46Ym"))
        time_text = _text(card.select_one(".JobCard_timeAndFlags__hHlJx"))
        posted = parse_relative_date(time_text)
        jobs.append(
            RawJob(
                source="talent",
                external_id=external_id,
                title=title,
                company=company,
                url=urljoin(BASE_URL, href),
                description=body,
                location=location or "Pakistan",
                remote=True if looks_remote(f"{title} {location}", [body]) else None,
                employment_type=infer_employment_type(body, time_text),
                salary=parse_talent_salary(body),
                posted_at=posted,
                tags=[],
                raw={"listing": body[:600], "time": time_text},
            )
        )
    return jobs


_CURRENCY_MARKER = re.compile(r"(?i)(?:PKR|Rs\.?|USD|EUR|GBP|\$|€|£|₹)")


def parse_talent_salary(text: str):
    """Talent.com Pakistan quotes monthly rupees unless a currency is shown.

    ``parse_salary("150k to 180k")`` has no currency to work with and defaults
    to USD/year, which would make a Rs 150,000/month role look like a US salary.
    """
    salary = parse_salary(text)
    if salary is None:
        return None
    lowered = (text or "").lower()
    if not _CURRENCY_MARKER.search(text or ""):
        period = "year" if re.search(r"\b(per annum|annum|annual|yearly)\b", lowered) else "month"
        salary = salary.model_copy(update={"currency": "PKR", "period": period})
    return salary


def parse_talent_detail(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    container = soup.select_one(
        "[class*='jobInfoContent'], [class*='jobDescriptionContainer'], article"
    )
    if container is None:
        return ""
    text = re.sub(r"\s+", " ", container.get_text(" ", strip=True)).strip()
    # Drop the repeated page furniture before the actual description.
    text = re.sub(r"^(?:Job description)\s*", "", text, flags=re.IGNORECASE)
    return text


def _view_id(href: str) -> str:
    try:
        values = parse_qs(urlsplit(href).query).get("id", [])
    except ValueError:
        return ""
    return values[0].strip() if values else ""


def _text(node) -> str:
    if node is None:
        return ""
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()


__all__ = ["TalentAdapter", "parse_talent_cards", "parse_talent_detail"]
