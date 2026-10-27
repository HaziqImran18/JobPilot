"""Talent.com adapter — public HTML search across supported country hosts.

Talent.com publishes one host per country (``pk.talent.com``, ``uk.talent.com``
and so on). The adapter picks the host from the requested location, falls back
to Pakistan for a blank request, and reads two result pages.

``ca.talent.com`` and ``us.talent.com`` are blocked or missing from some
networks; the registry reports that per source rather than pretending the
search succeeded.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup

from jobpilot.core.models import JobQuery, RawJob
from jobpilot.core.utils import parse_relative_date
from jobpilot.pipelines.categories import category_label
from jobpilot.pipelines.extract import infer_employment_type, parse_salary
from jobpilot.pipelines.locations import resolve_location
from jobpilot.sources.base import SourceAdapter, SourceError, looks_remote

BASE_URL = "https://pk.talent.com"
SEARCH_URL = f"{BASE_URL}/jobs"
MAX_PAGES = 2
COUNTRY_HOSTS: dict[str, str] = {
    "Pakistan": "pk.talent.com",
    "United Kingdom": "uk.talent.com",
    "United Arab Emirates": "ae.talent.com",
    "India": "in.talent.com",
    "Australia": "au.talent.com",
    "Singapore": "sg.talent.com",
    "Germany": "de.talent.com",
    "France": "fr.talent.com",
    "Netherlands": "nl.talent.com",
}


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
        location = resolve_location(query.location)
        country = location.country or "Pakistan"
        host = COUNTRY_HOSTS.get(country)
        if not host:
            # No dedicated host: use the global site rather than guessing a
            # country host that does not exist.
            host = "www.talent.com"
        base = f"https://{host}"
        keyword = (
            query.query.strip()
            or category_label(query.category)
            or "jobs"
        )
        place = location.display or ("Remote" if location.remote_only else country)
        params: dict[str, str] = {"k": keyword}
        if query.workplace_type == "remote" or query.remote is True:
            params["l"] = "Remote"
        else:
            params["l"] = place

        wanted = max(query.limit * 2, 20)
        jobs: list[RawJob] = []
        seen: set[str] = set()
        for page in range(1, MAX_PAGES + 1):
            page_params = dict(params)
            if page > 1:
                page_params["p"] = str(page)
            html = self.get_html(f"{base}/jobs", page_params)
            cards = parse_talent_cards(
                html,
                default_location=place,
                currency="PKR" if country == "Pakistan" else "",
            )
            if not cards:
                break
            for job in cards:
                if job.external_id in seen:
                    continue
                seen.add(job.external_id)
                jobs.append(job)
            if len(cards) < 15 or len(jobs) >= wanted:
                break

        if not jobs:
            return []
        jobs = jobs[:wanted]
        # Listing snippets are short. Read a few detail pages so matching has
        # real requirements, not just the teaser.
        for job in jobs[: min(len(jobs), 6)]:
            try:
                detail = self.get_html(job.url)
            except SourceError:
                continue
            description = parse_talent_detail(detail)
            if description and len(description) > len(job.description):
                job.description = description
                job.salary = job.salary or parse_talent_salary(
                    description[:6000], currency="PKR" if country == "Pakistan" else ""
                )
                job.posted_at = parse_relative_date(description[:400]) or job.posted_at
        return jobs


def parse_talent_cards(
    html: str, *, default_location: str = "Pakistan", currency: str = "PKR"
) -> list[RawJob]:
    soup = BeautifulSoup(html, "html.parser")
    host = html_base(html)
    base = f"https://{host}" if host else BASE_URL
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
                url=urljoin(base, href),
                description=body,
                location=location or default_location,
                remote=True if looks_remote(f"{title} {location}", [body]) else None,
                employment_type=infer_employment_type(body, time_text),
                salary=parse_talent_salary(body, currency=currency),
                posted_at=posted,
                tags=[],
                raw={"listing": body[:600], "time": time_text},
            )
        )
    return jobs


def html_base(html: str) -> str:
    """Best-effort host from a canonical link, used to make card URLs absolute."""
    match = re.search(r'<link[^>]+rel="canonical"[^>]+href="https?://([^/"]+)', html)
    if match:
        return match.group(1)
    match = re.search(r'https?://([a-z.]*talent\.com)', html)
    return match.group(1) if match else ""


_CURRENCY_MARKER = re.compile(r"(?i)(?:PKR|Rs\.?|USD|EUR|GBP|\$|€|£|₹)")


def parse_talent_salary(text: str, *, currency: str = "PKR"):
    """Talent.com Pakistan quotes monthly rupees unless a currency is shown.

    ``parse_salary("150k to 180k")`` has no currency to work with and defaults
    to USD/year, which would make a Rs 150,000/month role look like a US salary.
    """
    salary = parse_salary(text)
    if salary is None:
        return None
    if not currency:
        return salary
    lowered = (text or "").lower()
    if not _CURRENCY_MARKER.search(text or ""):
        period = "year" if re.search(r"\b(per annum|annum|annual|yearly)\b", lowered) else "month"
        salary = salary.model_copy(update={"currency": currency, "period": period})
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


__all__ = [
    "COUNTRY_HOSTS",
    "TalentAdapter",
    "parse_talent_cards",
    "parse_talent_detail",
    "parse_talent_salary",
]
