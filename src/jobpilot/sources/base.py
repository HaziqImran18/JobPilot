"""Source adapter protocol and shared HTTP behaviour."""

from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

from jobpilot.core.config import Settings, get_settings
from jobpilot.core.models import JobQuery, RawJob
from jobpilot.core.utils import utc_now_iso


class SourceError(RuntimeError):
    pass


@dataclass
class ProbeResult:
    name: str
    ok: bool
    detail: str = ""
    latency_ms: int | None = None
    checked_at: str = field(default_factory=utc_now_iso)


class SourceAdapter(ABC):
    name: str = "adapter"
    kind: str = "api"
    priority: int = 10

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._lock = threading.Lock()
        self._last_request = 0.0
        self._min_interval = 0.5

    # ---------------------------------------------------------------- transport
    def _throttle(self) -> None:
        with self._lock:
            wait = self._min_interval - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()

    def _client(self) -> httpx.Client:
        return httpx.Client(
            timeout=httpx.Timeout(self.settings.http_timeout),
            follow_redirects=True,
            headers={
                "User-Agent": self.settings.user_agent,
                "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
            },
        )

    def _html_client(self) -> httpx.Client:
        # Public job-board HTML often rejects non-browser user agents. These are
        # ordinary GET requests to public pages, not headless-browser sessions.
        return httpx.Client(
            timeout=httpx.Timeout(self.settings.http_timeout),
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        self._throttle()
        try:
            with self._client() as client:
                response = client.get(url, params=params)
                if response.status_code == 429:
                    time.sleep(2)
                    response = client.get(url, params=params)
                response.raise_for_status()
                return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SourceError(f"{self.name}: {exc}") from exc

    def get_text(self, url: str, params: dict[str, Any] | None = None) -> str:
        return self._get_text(url, params, self._client())

    def get_html(self, url: str, params: dict[str, Any] | None = None) -> str:
        return self._get_text(url, params, self._html_client())

    def _get_text(
        self,
        url: str,
        params: dict[str, Any] | None,
        client: httpx.Client,
    ) -> str:
        self._throttle()
        try:
            with client:
                response = client.get(url, params=params)
                response.raise_for_status()
                return response.text
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            hint = " (blocked automated access)" if status in {401, 403, 429} else ""
            raise SourceError(f"{self.name}: HTTP {status}{hint}") from exc
        except httpx.HTTPError as exc:
            raise SourceError(f"{self.name}: {exc}") from exc

    # ------------------------------------------------------------------- probe
    def probe(self) -> ProbeResult:
        started = time.perf_counter()
        try:
            self._probe()
            latency = int((time.perf_counter() - started) * 1000)
            return ProbeResult(name=self.name, ok=True, detail="reachable", latency_ms=latency)
        except Exception as exc:  # noqa: BLE001 - doctor reports the reason
            latency = int((time.perf_counter() - started) * 1000)
            return ProbeResult(name=self.name, ok=False, detail=str(exc)[:200], latency_ms=latency)

    @abstractmethod
    def _probe(self) -> None: ...

    @abstractmethod
    def fetch(self, query: JobQuery) -> list[RawJob]: ...

    def enabled(self) -> bool:
        return True


def looks_remote(location: str, tags: list[str] | None = None) -> bool:
    haystack = " ".join([location or "", *(tags or [])]).lower()
    markers = ("remote", "anywhere", "work from home", "wfh", "distributed", "worldwide")
    return any(marker in haystack for marker in markers)


class WebReader:
    """Fetches a single public page safely and extracts readable content."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    @staticmethod
    def validate_url(url: str) -> str:
        parts = urlsplit(url.strip())
        if parts.scheme not in {"http", "https"}:
            raise ValueError("Only http(s) URLs are allowed.")
        host = (parts.hostname or "").lower()
        if not host:
            raise ValueError("URL has no hostname.")
        blocked = ("localhost", "127.", "0.0.0.0", "::1", "169.254.", "10.", "192.168.")
        if host in {"localhost", "0.0.0.0", "::1"} or host.startswith(blocked):
            raise ValueError("Local and private addresses are not allowed.")
        if host.endswith(".local") or host.endswith(".internal"):
            raise ValueError("Internal hostnames are not allowed.")
        return url.strip()

    def fetch(self, url: str) -> tuple[str, str]:
        safe_url = self.validate_url(url)
        with httpx.Client(
            timeout=httpx.Timeout(self.settings.http_timeout),
            follow_redirects=True,
            headers={"User-Agent": self.settings.user_agent},
        ) as client:
            response = client.get(safe_url)
            response.raise_for_status()
            return response.text, str(response.url)
