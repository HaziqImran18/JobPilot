"""Shared helpers for MCP server processes."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from jobpilot.bootstrap import bootstrap


def ensure_ready() -> None:
    bootstrap()


def jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: jsonable(item) for key, item in value.items()}
    return value
