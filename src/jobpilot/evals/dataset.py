"""Dataset definitions for the offline eval harness."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_DATA = Path(__file__).resolve().parent / "data"


def _load(name: str) -> Any:
    with (_DATA / name).open(encoding="utf-8") as handle:
        return json.load(handle)


@dataclass(frozen=True)
class LabelledJob:
    external_id: str
    title: str
    company: str
    relevance: int
    ideal_score: float


@dataclass(frozen=True)
class InjectionCase:
    id: str
    text: str
    expected_flagged: bool


@dataclass(frozen=True)
class ToolCase:
    prompt: str
    expected_tools: tuple[str, ...]


@dataclass(frozen=True)
class JobDataset:
    name: str
    query: str
    description: str
    jobs: tuple[LabelledJob, ...]

    @property
    def relevance(self) -> dict[str, int]:
        return {job.external_id: job.relevance for job in self.jobs}

    @property
    def ideal_scores(self) -> dict[str, float]:
        return {job.external_id: job.ideal_score for job in self.jobs}


def load_job_dataset() -> JobDataset:
    payload = _load("jobs.json")
    jobs = tuple(
        LabelledJob(
            external_id=item["external_id"],
            title=item["title"],
            company=item["company"],
            relevance=int(item["relevance"]),
            ideal_score=float(item["ideal_score"]),
        )
        for item in payload["jobs"]
    )
    return JobDataset(
        name=payload["name"],
        query=payload["query"],
        description=payload["description"],
        jobs=jobs,
    )


def load_injection_cases() -> tuple[InjectionCase, ...]:
    return tuple(
        InjectionCase(
            id=item["id"],
            text=item["text"],
            expected_flagged=bool(item["expected_flagged"]),
        )
        for item in _load("injections.json")
    )


def load_tool_cases() -> tuple[ToolCase, ...]:
    return tuple(
        ToolCase(prompt=item["prompt"], expected_tools=tuple(item["expected_tools"]))
        for item in _load("tool_selection.json")
    )
