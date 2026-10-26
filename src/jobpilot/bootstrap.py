"""Idempotent startup for shared auth storage and per-user workspaces."""

from __future__ import annotations

import threading

from jobpilot.core.auth import AuthStore
from jobpilot.core.config import user_settings
from jobpilot.core.db import get_db
from jobpilot.core.user_context import user_context
from jobpilot.harness.permissions import ApprovalBroker

_ready: set[str] = set()
_ready_lock = threading.Lock()


def bootstrap() -> None:
    """Create the shared account table. No profile or job data is seeded."""
    AuthStore().ensure_schema()


def bootstrap_workspace(user_id: str) -> None:
    """Prepare one account's private database exactly once per process."""
    if not user_id:
        return
    with _ready_lock:
        if user_id in _ready:
            return
    user_settings(user_id).ensure_dirs()
    with user_context(user_id):
        db = get_db()
        db.init()
        ApprovalBroker(db).expire_stale()
    with _ready_lock:
        _ready.add(user_id)


__all__ = ["bootstrap", "bootstrap_workspace"]
