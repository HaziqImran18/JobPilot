"""Indeed adapter — best-effort public RSS/HTML reading.

Indeed has no free public API and its pages are fronted by Cloudflare. From many
networks every request returns HTTP 403 ("Security Check"), including the RSS
feed. The adapter still tries the public endpoints, because some networks are
allowed through, and reports the block explicitly instead of silently returning
an empty list. JobPilot never bypasses a challenge or signs in.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urljoin

import feedparser
from bs4 import BeautifulSoup

from jobpilot.core.models import JobQuery, RawJob
from jobpilot.core.utils import parse_datetime, stable_hash
from jobpilot.pipelines.extract import infer_employment_type, parse_salary
from jobpilot.sources.base import SourceAdapter, SourceError, looks_remote

BASE_URL = "https://www.indeed.com"
RSS_URL = f"{BASE_URL}/rss"
SEARCH_URL = f"{BASE_URL}/jobs"
BLOCKED_HINT = (
    "Indeed blocks automated access from this network (Cloudflare HTTP 403); "
    "use the Indeed link in the UI or paste a posting manually."
)


class IndeedAdapter(SourceAdapter):
    name = "indeed"
    kind = "html"
    priority = 8
    _min_interval = 0.8

    def enabled(self) -> bool:
        return self.settings.enable_indeed

    def _probe(self) -> None:
        try:
            self._rss("software engineer", "Pakistan")
        except SourceError as exc:
            raise SourceError(_block_message(exc)) from exc

    def fetch(self, query: JobQuery) -> list[RawJob]:
        keywords = query.query.strip() or "software engineer"
        location = query.location.strip() or "Pakistan"
        last_error: SourceError | None = None
        try:
            jobs = self._rss(keywords, location)
            if jobs:
                return jobs[: max(query.limit * 2, 20)]
        except SourceError as exc:
            last_error = exc
        try:
            jobs = self._html(keywords, location)
            if jobs:
                return jobs[: max(query.limit * 2, 20)]
        except SourceError as exc:
            last_error = exc
        raise SourceError(_block_message(last_error) if last_error else BLOCKED_HINT)

    def _rss(self, keywords: str, location: str) -> list[RawJob]:
        text = self.get_html(RSS_URL, {"q": keywords, "l": location, "sort": "date"})
        parsed = feedparser.parse(text)
        if not parsed.entries:
            if "security check" in text.lower() or "captcha" in text.lower():
                raise SourceError(BLOCKED_HINT)
            return []
        jobs: list[RawJob] = []
        for entry in parsed.entries[:40]:
            title = str(entry.get("title", "")).strip()
            link = str(entry.get("link", "")).strip()
            if not title or not link:
                continue
            summary = entry.get("summary", "") or entry.get("description", "")
            jobs.append(
                RawJob(
                    source=self.name,
                    external_id=str(entry.get("id") or stable_hash(link, length=24)),
                    title=title,
                    company=str(entry.get("author") or "Unknown company"),
                    url=link,
                    description=summary,
                    location=location,
                    remote=True if looks_remote(f"{title} {location}", [summary]) else None,
                    employment_type=infer_employment_type(title, summary),
                    salary=parse_salary(summary),
                    posted_at=parse_datetime(entry.get("published")),
                    tags=[],
                    raw={"via": "rss"},
                )
            )
        return jobs

    def _html(self, keywords: str, location: str) -> list[RawJob]:
        html = self.get_html(SEARCH_URL, {"q": keywords, "l": location, "sort": "date"})
        jobs = parse_indeed_cards(html)
        if jobs:
            return jobs
        if "security check" in html.lower() or "captcha" in html.lower():
            raise SourceError(BLOCKED_HINT)
        return []


def parse_indeed_cards(html: str) -> list[RawJob]:
    """Parse a public Indeed result page, including its embedded JSON blob."""
    soup = BeautifulSoup(html, "html.parser")
    jobs = _parse_cards(soup)
    if jobs:
        return jobs
    return _parse_embedded(soup)


def _parse_cards(soup: BeautifulSoup) -> list[RawJob]:
    jobs: list[RawJob] = []
    for card in soup.select("div.job_seen_beacon, li.css-5lfssm, div[data-jk]"):
        link = card.select_one("h2.jobTitle a, a[data-jk], a[id^='job_']")
        if link is None:
            continue
        href = link.get("href", "")
        external_id = (
            card.get("data-jk")
            or link.get("data-jk")
            or _jk_from_href(href)
            or stable_hash(href, length=20)
        )
        title = _text(link) or _text(card.select_one("h2, .jobTitle"))
        if not title:
            continue
        company = _text(
            card.select_one("[data-testid='company-name'], .companyName, span.company")
        )
        location = _text(
            card.select_one("[data-testid='text-location'], .companyLocation, div.companyLocation")
        )
        summary = _text(card.select_one(".job-snippet, [data-testid='belowJobSnippet'], .summary"))
        salary_text = _text(
            card.select_one("[data-testid='attribute_snippet_testid'], .salary-snippet-container")
        )
        date_text = _text(
            card.select_one(
                "[data-testid='myJobsStateDate'], span.date, .date, "
                "span[data-testid='myJobsStateDate']"
            )
        )
        jobs.append(
            RawJob(
                source="indeed",
                external_id=str(external_id),
                title=title,
                company=company or "Unknown company",
                url=urljoin(BASE_URL, href.split("&vjs=")[0]),
                description=summary,
                location=location,
                remote=None,
                employment_type=infer_employment_type(title, summary),
                salary=parse_salary(salary_text or summary),
                posted_at=parse_datetime(date_text),
                tags=[],
                raw={"via": "html", "date": date_text},
            )
        )
    return jobs


def _parse_embedded(soup: BeautifulSoup) -> list[RawJob]:
    """Indeed embeds result data in ``window.mosaic.providerData``."""
    script_text = " ".join(script.get_text() for script in soup.find_all("script"))
    match = re.search(
        r'"mosaic-provider-jobcards"\s*:\s*(\{.*?\})\s*,\s*"mosaic-provider-',
        script_text,
        re.DOTALL,
    )
    if not match:
        return []
    try:
        payload = json.loads(match.group(1))
    except (ValueError, TypeError):
        return []
    results = (
        payload.get("metaData", {}).get("mosaicProviderJobCardsModel", {}).get("results", [])
    )
    jobs: list[RawJob] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        jobkey = str(item.get("jobkey") or "")
        title = str(item.get("displayTitle") or item.get("title") or "").strip()
        if not jobkey or not title:
            continue
        summary = str(item.get("snippet") or "")
        jobs.append(
            RawJob(
                source="indeed",
                external_id=jobkey,
                title=title,
                company=str(item.get("company") or "Unknown company"),
                url=f"{BASE_URL}/viewjob?jk={jobkey}",
                description=summary,
                location=str(item.get("formattedLocation") or ""),
                remote=True
                if looks_remote(
                    f"{title} {item.get('formattedLocation') or ''}", [summary]
                )
                else None,
                employment_type=infer_employment_type(title, summary),
                salary=parse_salary(summary),
                posted_at=parse_datetime(item.get("pubDate")),
                tags=[],
                raw={"via": "embedded"},
            )
        )
    return jobs


def _block_message(error: SourceError | None) -> str:
    detail = str(error) if error else ""
    if "403" in detail or "401" in detail or "429" in detail or "security" in detail.lower():
        return BLOCKED_HINT
    return detail or BLOCKED_HINT


def _jk_from_href(href: str) -> str:
    match = re.search(r"[?&]jk=([A-Za-z0-9]+)", href)
    return match.group(1) if match else ""


def _text(node) -> str:
    if node is None:
        return ""
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()


__all__ = ["IndeedAdapter", "parse_indeed_cards"]
