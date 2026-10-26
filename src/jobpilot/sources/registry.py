"""Source registry: fan out to healthy adapters and dedupe the results."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass, field

from jobpilot.core.config import Settings, get_settings
from jobpilot.core.models import JobQuery, RawJob
from jobpilot.pipelines.extract import dedupe, to_job
from jobpilot.sources.arbeitnow import ArbeitnowAdapter
from jobpilot.sources.base import ProbeResult, SourceAdapter
from jobpilot.sources.himalayas import HimalayasAdapter
from jobpilot.sources.indeed import IndeedAdapter
from jobpilot.sources.jobicy import JobicyAdapter
from jobpilot.sources.linkedin import LinkedInAdapter
from jobpilot.sources.manual import ManualAdapter
from jobpilot.sources.mustakbil import MustakbilAdapter
from jobpilot.sources.remoteok import RemoteOKAdapter
from jobpilot.sources.remotive import RemotiveAdapter
from jobpilot.sources.rss import RSSAdapter
from jobpilot.sources.talent import TalentAdapter


@dataclass
class FetchReport:
    jobs: list = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    duration_ms: int = 0

    @property
    def total_fetched(self) -> int:
        return sum(self.counts.values())

    @property
    def sources_used(self) -> list[str]:
        return sorted(name for name, count in self.counts.items() if count)


class SourceRegistry:
    """Runs independent sources concurrently.

    Priority order decides which duplicate copy is kept. Sources are independent
    (a Remotive outage should not block Arbeitnow), so we fan out instead of a
    strict first-healthy-wins chain. Each source still has its own fallback
    behaviour, and `doctor` reports health per source.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.adapters: list[SourceAdapter] = [
            RemotiveAdapter(self.settings),
            ArbeitnowAdapter(self.settings),
            RemoteOKAdapter(self.settings),
            JobicyAdapter(self.settings),
            HimalayasAdapter(self.settings),
            RSSAdapter(self.settings),
            LinkedInAdapter(self.settings),
            MustakbilAdapter(self.settings),
            TalentAdapter(self.settings),
            IndeedAdapter(self.settings),
        ]
        self.manual = ManualAdapter(self.settings)

    def enabled_adapters(self) -> list[SourceAdapter]:
        return sorted(
            [adapter for adapter in self.adapters if adapter.enabled()],
            key=lambda adapter: adapter.priority,
        )

    def fetch_all(self, query: JobQuery) -> FetchReport:
        started = time.perf_counter()
        adapters = self.enabled_adapters()
        if query.sources:
            requested = {source.lower() for source in query.sources}
            adapters = [adapter for adapter in adapters if adapter.name in requested]
        report = FetchReport()
        if not adapters:
            report.duration_ms = int((time.perf_counter() - started) * 1000)
            return report

        results: dict[str, list[RawJob]] = {}
        with ThreadPoolExecutor(max_workers=min(10, len(adapters))) as pool:
            futures = {adapter.name: pool.submit(adapter.fetch, query) for adapter in adapters}
            for name, future in futures.items():
                try:
                    jobs = future.result(timeout=self.settings.http_timeout + 5)
                    results[name] = jobs
                    report.counts[name] = len(jobs)
                except FuturesTimeout:
                    report.errors[name] = "timed out"
                    report.counts[name] = 0
                except Exception as exc:  # noqa: BLE001 - one bad source must not break search
                    report.errors[name] = str(exc)[:200]
                    report.counts[name] = 0

        raw_jobs: list[RawJob] = []
        for adapter in adapters:
            raw_jobs.extend(results.get(adapter.name, []))
        normalised = [to_job(raw) for raw in raw_jobs if raw.title and raw.url]
        report.jobs = dedupe(normalised)
        report.duration_ms = int((time.perf_counter() - started) * 1000)
        return report

    def probe_all(self, timeout: float = 12.0) -> list[ProbeResult]:
        # Disabled adapters are not failures. Every enabled adapter is probed;
        # boards that block automated access (Indeed, Rozee, Bayt) report the
        # block here instead of silently disappearing from the source list.
        adapters = [*self.enabled_adapters(), self.manual]
        results: list[ProbeResult] = []
        with ThreadPoolExecutor(max_workers=len(adapters)) as pool:
            futures = {adapter.name: pool.submit(adapter.probe) for adapter in adapters}
            for name, future in futures.items():
                try:
                    results.append(future.result(timeout=timeout))
                except FuturesTimeout:
                    results.append(ProbeResult(name=name, ok=False, detail="probe timed out"))
                except Exception as exc:  # noqa: BLE001
                    results.append(ProbeResult(name=name, ok=False, detail=str(exc)[:180]))
        return results


_registry: SourceRegistry | None = None


def get_registry() -> SourceRegistry:
    global _registry
    if _registry is None:
        _registry = SourceRegistry()
    return _registry


__all__ = ["FetchReport", "SourceRegistry", "get_registry"]
