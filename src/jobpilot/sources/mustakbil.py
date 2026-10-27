"""Mustakbil adapter — Pakistan's long-running general job board.

Mustakbil's keyword search endpoint now returns HTTP 410, but its public
category and city pages are server-rendered and include a useful summary,
salary, location and posting age on every card. We read the technology
categories (or the requested Pakistani city) and let the local full-text index
narrow the results for a query.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from jobpilot.core.models import JobQuery, RawJob
from jobpilot.core.utils import parse_datetime, parse_relative_date
from jobpilot.pipelines.extract import infer_employment_type, parse_salary
from jobpilot.sources.base import SourceAdapter, SourceError, looks_remote

BASE_URL = "https://www.mustakbil.com"
TECH_CATEGORIES = (
    "information-technology",
    "computer-software",
    "web-e-commerce",
)
# Verified category slugs on the public server-rendered pages (October 2026).
# Keyword search and ?page=2 both return HTTP 410, so coverage comes from reading
# the right category/city pages rather than paginating.
CATEGORY_SLUGS: dict[str, tuple[str, ...]] = {
    "software": TECH_CATEGORIES,
    "ai_ml": ("information-technology", "computer-software"),
    "data": ("information-technology", "computer-software"),
    "devops": ("information-technology", "computer-software"),
    "security": ("information-technology", "computer-software"),
    "engineering": ("engineering", "information-technology"),
    "finance": ("accounting", "finance"),
    "banking": ("accounting", "finance"),
    "accounting": ("accounting", "finance"),
    "sales": ("sales", "advertising-marketing-public-relations"),
    "marketing": ("advertising-marketing-public-relations", "media"),
    "hr": ("human-resources-recruiting", "administrative"),
    "support": ("customer-service-and-call-centre", "administrative"),
    "operations": ("supply-chain", "procurement", "administrative"),
    "management": ("management", "administrative"),
    "healthcare": ("healthcare",),
    "education": ("education",),
    "legal": ("legal",),
    "design": ("media", "information-technology"),
    "admin": ("administrative", "customer-service-and-call-centre"),
    "other": ("accounting", "sales", "information-technology"),
}
DEFAULT_CATEGORIES = ("information-technology", "accounting", "sales")
CITY_SLUGS = {
    "islamabad": "islamabad",
    "rawalpindi": "rawalpindi",
    "karachi": "karachi",
    "lahore": "lahore",
    "faisalabad": "faisalabad",
    "multan": "multan",
    "peshawar": "peshawar",
    "quetta": "quetta",
    "hyderabad": "hyderabad",
    "gujranwala": "gujranwala",
    "sialkot": "sialkot",
    "bahawalpur": "bahawalpur",
    "sargodha": "sargodha",
    "abbottabad": "abbottabad",
    "sukkur": "sukkur",
}


class MustakbilAdapter(SourceAdapter):
    name = "mustakbil"
    kind = "html"
    priority = 6
    _min_interval = 0.35

    def enabled(self) -> bool:
        return self.settings.enable_mustakbil

    def _probe(self) -> None:
        html = self.get_html(f"{BASE_URL}/jobs/pakistan/information-technology")
        if not parse_mustakbil_cards(html):
            raise SourceError("Unexpected Mustakbil page layout")

    def fetch(self, query: JobQuery) -> list[RawJob]:
        urls = _category_urls(query)
        if not urls:
            return []
        jobs: list[RawJob] = []
        seen: set[str] = set()
        for url in urls:
            html = self.get_html(url)
            for job in parse_mustakbil_cards(html, category=url.rsplit("/", 1)[-1]):
                if job.external_id in seen:
                    continue
                seen.add(job.external_id)
                jobs.append(job)
                if len(jobs) >= max(query.limit * 2, 20):
                    return jobs
        if not jobs:
            raise SourceError("Mustakbil returned no job cards")
        return jobs


def _category_urls(query: JobQuery) -> list[str]:
    location = (query.location or "").strip().lower()
    first = re.split(r"\s*,\s*", location, maxsplit=1)[0].strip()
    if query.remote is True or query.workplace_type == "remote" or first in {
        "remote",
        "anywhere",
        "worldwide",
        "online",
    }:
        return [f"{BASE_URL}/jobs/pakistan/online"]

    slugs: list[str] = []
    if query.category:
        slugs.extend(CATEGORY_SLUGS.get(query.category, ()))  # type: ignore[arg-type]
    if not slugs:
        slugs.extend(DEFAULT_CATEGORIES)
    slugs = list(dict.fromkeys(slug for slug in slugs if slug))[:4]

    if first and first not in {"pakistan", "pk"}:
        city = CITY_SLUGS.get(first)
        if not city:
            return []  # Mustakbil only publishes Pakistani roles.
        # A city page is the broadest view of that city. Add one category page
        # so a niche occupation still shows up when the city feed is busy.
        return [f"{BASE_URL}/jobs/pakistan/{city}"] + [
            f"{BASE_URL}/jobs/pakistan/{slug}" for slug in slugs[:1]
        ]
    urls = [f"{BASE_URL}/jobs/pakistan/{slug}" for slug in slugs]
    # The all-country page catches roles whose category we did not name.
    urls.append(f"{BASE_URL}/jobs/pakistan")
    return list(dict.fromkeys(urls))


def parse_mustakbil_cards(html: str, *, category: str = "") -> list[RawJob]:
    """Turn server-rendered Mustakbil cards into raw jobs."""
    soup = BeautifulSoup(html, "html.parser")
    jobs: list[RawJob] = []
    for card in soup.select("article.jl-card, article.jc-card"):
        title_link = card.select_one(".jc-title__link")
        if title_link is None:
            continue
        href = title_link.get("href") or ""
        external_id = card.get("id") or href.rstrip("/").rsplit("/", 1)[-1]
        if not external_id:
            continue
        company = _text(card.select_one(".jc-byline__name")) or "Unknown company"
        location = _text(card.select_one(".jc-byline__place"))
        description = _text(card.select_one(".jc-desc"))
        facts = [_text(node) for node in card.select(".jc-fact") if _text(node)]
        time_text = _text(card.select_one(".jc-fact--time"))
        time_node = card.select_one("time")
        posted = parse_relative_date(time_text)
        if posted is None and time_node is not None:
            posted = parse_datetime(time_node.get("datetime"))
        salary_text = _text(card.select_one(".jc-pay"))
        salary = parse_salary(salary_text)
        if (
            salary
            and salary.currency == "PKR"
            and salary.period == "year"
            and "year" not in salary_text.lower()
            and "annum" not in salary_text.lower()
        ):
            # Pakistani boards quote monthly pay unless they say otherwise.
            salary = salary.model_copy(update={"period": "month"})
        title = _text(title_link)
        is_online = category == "online" or title.lower().startswith("online ")
        remote = True if is_online or looks_remote(location, facts) else None
        jobs.append(
            RawJob(
                source="mustakbil",
                external_id=str(external_id),
                title=title,
                company=company,
                url=urljoin(BASE_URL, href),
                description=description or f"{title} at {company} in {location}.",
                location=location,
                remote=remote,
                employment_type=infer_employment_type(*facts, description),
                salary=salary,
                posted_at=posted,
                tags=[fact for fact in facts if not re.search(r"\bago\b", fact, re.I)],
                raw={"category": category, "facts": facts, "salary_text": salary_text},
            )
        )
    return jobs


def _text(node) -> str:
    if node is None:
        return ""
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()


__all__ = ["MustakbilAdapter", "parse_mustakbil_cards"]
