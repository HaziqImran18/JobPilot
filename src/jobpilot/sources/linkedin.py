"""LinkedIn adapter placeholder — deliberately disabled.

Automated scraping of LinkedIn violates its Terms of Service and can get an
account restricted. JobPilot works without it; the adapter exists only to make
the boundary explicit in `doctor` output.
"""

from __future__ import annotations

from jobpilot.core.models import JobQuery, RawJob
from jobpilot.sources.base import ProbeResult, SourceAdapter


class LinkedInAdapter(SourceAdapter):
    name = "linkedin"
    kind = "browser"
    priority = 99

    def enabled(self) -> bool:
        return self.settings.enable_linkedin

    def _probe(self) -> None:
        if not self.enabled():
            return

    def probe(self) -> ProbeResult:
        return ProbeResult(
            name=self.name,
            ok=False,
            detail="Disabled by design: LinkedIn automation violates their terms. Use manual paste instead.",
            latency_ms=0,
        )

    def fetch(self, query: JobQuery) -> list[RawJob]:
        return []
