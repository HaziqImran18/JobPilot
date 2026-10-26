"""Offline fixtures for the evaluation harness and tests.

These records are deliberately synthetic and live inside the package. The
runtime application never imports this module: a real account starts with a
blank profile and only receives jobs from live sources.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jobpilot.core.db import Database
from jobpilot.core.models import Profile, RawJob
from jobpilot.pipelines.extract import to_job
from jobpilot.services.jobs import JobService
from jobpilot.services.profile import ProfileService

_DATA = Path(__file__).resolve().parent / "data"


def _load(name: str) -> Any:
    with (_DATA / name).open(encoding="utf-8") as handle:
        return json.load(handle)


def load_eval_profile() -> Profile:
    return Profile.model_validate(_load("profile.json"))


def load_eval_jobs() -> list[RawJob]:
    return [RawJob.model_validate(item) for item in _load("eval_jobs.json")]


def seed_eval_workspace(db: Database) -> None:
    """Populate a throw-away database with the labelled evaluation corpus."""
    db.init()
    ProfileService(db).save(load_eval_profile())
    JobService(db).upsert([to_job(raw) for raw in load_eval_jobs()])


__all__ = ["load_eval_jobs", "load_eval_profile", "seed_eval_workspace"]
