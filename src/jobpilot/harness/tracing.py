"""JSONL tracing: every agent run leaves an auditable artifact on disk."""

from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from typing import Any

from jobpilot.core.config import current_settings
from jobpilot.core.models import TraceSummary
from jobpilot.core.utils import json_dumps, utc_now_iso


class RunTrace:
    """Append-only JSONL writer for a single run."""

    def __init__(self, run_id: str, session_id: str, path: Path) -> None:
        self.run_id = run_id
        self.session_id = session_id
        self.path = path
        self.started_at = utc_now_iso()
        self.ended_at = ""
        self.steps = 0
        self.tool_calls = 0
        self.cost_usd = 0.0
        self.tokens = 0
        self.status = "running"
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._append("run_start", {"session_id": session_id, "started_at": self.started_at})

    def _append(self, event_type: str, data: dict[str, Any]) -> None:
        record = {"ts": utc_now_iso(), "type": event_type, "run_id": self.run_id, **data}
        line = json_dumps(record)
        with self._lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def event(self, event_type: str, **data: Any) -> None:
        self._append(event_type, data)

    def llm_call(
        self,
        model: str,
        purpose: str,
        prompt_tokens: int,
        completion_tokens: int,
        cost_usd: float,
        latency_ms: int,
        cached: bool,
    ) -> None:
        self.cost_usd += cost_usd
        self.tokens += prompt_tokens + completion_tokens
        self._append(
            "llm_call",
            {
                "model": model,
                "purpose": purpose,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cost_usd": round(cost_usd, 6),
                "latency_ms": latency_ms,
                "cached": cached,
            },
        )

    def tool_call(self, name: str, arguments: dict[str, Any], tier: str) -> None:
        self.tool_calls += 1
        self._append("tool_call", {"name": name, "arguments": arguments, "tier": tier})

    def tool_result(self, name: str, ok: bool, duration_ms: int, summary: str) -> None:
        self._append(
            "tool_result",
            {"name": name, "ok": ok, "duration_ms": duration_ms, "summary": summary[:500]},
        )

    def step(self, index: int, note: str = "") -> None:
        self.steps = max(self.steps, index + 1)
        self._append("step", {"index": index, "note": note})

    def finish(self, status: str = "completed") -> None:
        if self.status != "running":
            return
        self.status = status
        self.ended_at = utc_now_iso()
        self._append(
            "run_end",
            {
                "status": status,
                "ended_at": self.ended_at,
                "steps": self.steps,
                "tool_calls": self.tool_calls,
                "cost_usd": round(self.cost_usd, 6),
                "tokens": self.tokens,
            },
        )

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "steps": self.steps,
            "tool_calls": self.tool_calls,
            "cost_usd": round(self.cost_usd, 6),
            "tokens": self.tokens,
            "status": self.status,
        }


class TraceStore:
    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or current_settings().traces_dir
        self.directory.mkdir(parents=True, exist_ok=True)

    def start(self, session_id: str = "") -> RunTrace:
        run_id = (
            f"run_{utc_now_iso()[:19].replace(':', '').replace('-', '')}_{uuid.uuid4().hex[:6]}"
        )
        return RunTrace(run_id, session_id, self.directory / f"{run_id}.jsonl")

    def list_runs(self, limit: int = 30) -> list[TraceSummary]:
        files = sorted(
            self.directory.glob("run_*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
        )
        summaries: list[TraceSummary] = []
        for path in files[:limit]:
            events = self._read(path)
            if not events:
                continue
            start = next((e for e in events if e.get("type") == "run_start"), {})
            end = next((e for e in reversed(events) if e.get("type") == "run_end"), {})
            llm_calls = [e for e in events if e.get("type") == "llm_call"]
            summaries.append(
                TraceSummary(
                    run_id=path.stem,
                    session_id=str(start.get("session_id", "")),
                    started_at=str(start.get("started_at", "")),
                    ended_at=str(end.get("ended_at", "")),
                    steps=int(end.get("steps", 0)),
                    tool_calls=int(end.get("tool_calls", 0)),
                    cost_usd=round(sum(float(e.get("cost_usd", 0)) for e in llm_calls), 6),
                    tokens=sum(
                        int(e.get("prompt_tokens", 0)) + int(e.get("completion_tokens", 0))
                        for e in llm_calls
                    ),
                    status=str(end.get("status", "running")),
                )
            )
        return summaries

    def read(self, run_id: str) -> list[dict[str, Any]]:
        safe_id = "".join(ch for ch in run_id if ch.isalnum() or ch in "_-")
        path = self.directory / f"{safe_id}.jsonl"
        if not path.exists():
            return []
        return self._read(path)

    @staticmethod
    def _read(path: Path) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            return []
        return events
