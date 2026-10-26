"""Manual source: paste a job URL or raw text and extract structured fields."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from jobpilot.core.models import JobQuery, RawJob, Salary
from jobpilot.core.utils import html_to_text, parse_datetime, stable_hash, truncate
from jobpilot.pipelines.extract import parse_salary
from jobpilot.sources.base import ProbeResult, SourceAdapter, WebReader


class ManualAdapter(SourceAdapter):
    name = "manual"
    kind = "manual"
    priority = 1

    def __init__(self, settings=None) -> None:
        super().__init__(settings)
        self.reader = WebReader(self.settings)

    def enabled(self) -> bool:
        return True

    def _probe(self) -> None:
        return None

    def fetch(self, query: JobQuery) -> list[RawJob]:
        return []  # manual entries are added explicitly

    def probe(self) -> ProbeResult:
        return ProbeResult(name=self.name, ok=True, detail="always available", latency_ms=0)

    # ------------------------------------------------------------------- URL
    def from_url(self, url: str) -> RawJob:
        html, final_url = self.reader.fetch(url)
        return self._parse_page(html, final_url)

    def _parse_page(self, html: str, url: str) -> RawJob:
        soup = BeautifulSoup(html, "html.parser")
        posting = _find_job_posting(soup)
        if posting:
            return _from_json_ld(posting, url)

        title = _meta(soup, "og:title") or (soup.title.get_text(strip=True) if soup.title else "")
        description = _meta(soup, "og:description") or _meta(soup, "description")
        site = _meta(soup, "og:site_name") or urlsplit(url).netloc
        body = soup.find("main") or soup.find("article") or soup.body
        body_text = html_to_text(str(body))[:10000] if body else description
        company = site.replace("www.", "").split(".")[0].title()
        return RawJob(
            source=self.name,
            external_id=stable_hash(url, length=24),
            title=title or "Untitled role",
            company=company,
            url=url,
            description=body_text or description,
            location=_meta(soup, "og:locality") or "",
            remote=None,
            salary=parse_salary(body_text[:5000]),
            raw={"extraction": "opengraph"},
        )

    def from_text(self, text: str, *, company: str = "", title: str = "", url: str = "") -> RawJob:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        first = title or (lines[0] if lines else "Pasted job")
        body = "\n".join(lines[1:]) if not title and len(lines) > 1 else text
        return RawJob(
            source=self.name,
            external_id=stable_hash(text[:2000], length=24),
            title=first[:180],
            company=company or "Pasted listing",
            url=url,
            description=truncate(body, 12000),
            remote="remote" in text.lower() or None,
            salary=parse_salary(text[:5000]),
            raw={"extraction": "text"},
        )


def _meta(soup: BeautifulSoup, name: str) -> str:
    tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
    return (tag.get("content") or "").strip() if tag else ""


def _find_job_posting(soup: BeautifulSoup) -> dict[str, Any] | None:
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for candidate in _walk_json_ld(data):
            if isinstance(candidate, dict):
                types = candidate.get("@type")
                if types == "JobPosting" or (isinstance(types, list) and "JobPosting" in types):
                    return candidate
    return None


def _walk_json_ld(data: Any):
    if isinstance(data, list):
        for item in data:
            yield from _walk_json_ld(item)
    elif isinstance(data, dict):
        yield data
        if "@graph" in data:
            yield from _walk_json_ld(data["@graph"])


def _from_json_ld(posting: dict[str, Any], url: str) -> RawJob:
    company = ""
    org = posting.get("hiringOrganization")
    if isinstance(org, dict):
        company = org.get("name", "")
    description = html_to_text(str(posting.get("description", "")))
    location = _json_ld_location(posting)
    salary = _json_ld_salary(posting)
    employment = posting.get("employmentType", "")
    if isinstance(employment, list):
        employment = ", ".join(str(item) for item in employment)
    return RawJob(
        source="manual",
        external_id=stable_hash(posting.get("identifier") or url, length=24),
        title=str(posting.get("title", "Untitled role")),
        company=company,
        url=str(posting.get("url") or url),
        description=description,
        location=location,
        remote="remote" in (location + " " + description[:500]).lower(),
        employment_type=str(employment),
        salary=salary,
        posted_at=parse_datetime(posting.get("datePosted")),
        tags=posting.get("industry") if isinstance(posting.get("industry"), list) else [],
        raw={"extraction": "json-ld"},
    )


def _json_ld_location(posting: dict[str, Any]) -> str:
    locations = posting.get("jobLocation") or []
    if isinstance(locations, dict):
        locations = [locations]
    parts: list[str] = []
    for item in locations if isinstance(locations, list) else []:
        address = item.get("address", {}) if isinstance(item, dict) else {}
        if isinstance(address, str):
            parts.append(address)
        elif isinstance(address, dict):
            parts.extend(
                str(address.get(key))
                for key in ("addressLocality", "addressRegion", "addressCountry")
                if address.get(key)
            )
    if not parts and posting.get("jobLocationType") == "TELECOMMUTE":
        return "Remote"
    return ", ".join(dict.fromkeys(parts))


def _json_ld_salary(posting: dict[str, Any]) -> Salary | None:
    base = posting.get("baseSalary")
    if not isinstance(base, dict):
        return None
    currency = str(base.get("currency") or "USD")
    value = base.get("value")
    unit = "year"
    if isinstance(value, dict):
        unit = str(value.get("unitText") or "YEAR").lower()
        unit = {"hour": "hour", "month": "month", "year": "year"}.get(unit.rstrip("ly"), "year")
        try:
            minimum = float(value["minValue"]) if value.get("minValue") is not None else None
            maximum = float(value["maxValue"]) if value.get("maxValue") is not None else None
        except (TypeError, ValueError):
            return None
        return Salary(minimum=minimum, maximum=maximum, currency=currency, period=unit)
    try:
        return Salary(minimum=float(value), maximum=float(value), currency=currency, period="year")
    except (TypeError, ValueError):
        return None
