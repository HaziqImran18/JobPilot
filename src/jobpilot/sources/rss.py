"""RSS adapter for public job feeds (no key, no scraping of private pages)."""

from __future__ import annotations

import feedparser

from jobpilot.core.models import JobQuery, RawJob
from jobpilot.core.utils import parse_datetime, stable_hash
from jobpilot.sources.base import SourceAdapter, SourceError

FEEDS: list[tuple[str, str]] = [
    ("Remotive", "https://remotive.com/remote-jobs/feed"),
    ("We Work Remotely", "https://weworkremotely.com/categories/remote-programming-jobs.rss"),
]


class RSSAdapter(SourceAdapter):
    name = "rss"
    kind = "feed"
    priority = 5
    _min_interval = 1.0

    def enabled(self) -> bool:
        return self.settings.enable_rss

    def _probe(self) -> None:
        xml = self.get_text(FEEDS[0][1])
        if "<rss" not in xml.lower() and "<feed" not in xml.lower():
            raise SourceError("RSS feed returned unexpected content")

    def fetch(self, query: JobQuery) -> list[RawJob]:
        if query.remote is False:
            return []
        terms = [term.lower() for term in query.query.split() if len(term) > 2]
        jobs: list[RawJob] = []
        for feed_name, feed_url in FEEDS:
            try:
                xml = self.get_text(feed_url)
            except SourceError:
                continue
            parsed = feedparser.parse(xml)
            for entry in parsed.entries[:40]:
                title = entry.get("title", "").strip()
                summary = entry.get("summary", "") or entry.get("description", "")
                haystack = f"{title} {summary}".lower()
                if terms and not any(term in haystack for term in terms):
                    continue
                link = entry.get("link", "")
                if not link:
                    continue
                company, clean_title = _split_company(title, feed_name)
                jobs.append(
                    RawJob(
                        source=self.name,
                        external_id=entry.get("id") or stable_hash(link, length=24),
                        title=clean_title,
                        company=company,
                        url=link,
                        description=summary,
                        location="Remote",
                        remote=True,
                        posted_at=parse_datetime(entry.get("published") or entry.get("updated")),
                        tags=[
                            tag.get("term", "") for tag in entry.get("tags", []) if tag.get("term")
                        ],
                        raw={"feed": feed_name},
                    )
                )
            if len(jobs) >= query.limit * 2:
                break
        return jobs


def _split_company(title: str, feed_name: str) -> tuple[str, str]:
    for separator in (" : ", ": ", " at "):
        if separator in title:
            left, right = title.split(separator, 1)
            if separator.strip() == "at":
                return right.strip(), left.strip()
            return left.strip(), right.strip()
    return feed_name, title
