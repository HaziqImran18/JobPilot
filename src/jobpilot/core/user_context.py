"""Request and process scoped user identity.

The web app, CLI and MCP subprocesses all need to know which account owns the
data they are touching. A context variable keeps that concern out of every
storage call while remaining explicit at the request boundary.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_current_user: ContextVar[str] = ContextVar("jobpilot_user_id", default="")


def current_user_id() -> str:
    return _current_user.get()


def set_current_user(user_id: str) -> None:
    _current_user.set(user_id)


def clear_current_user() -> None:
    _current_user.set("")


@contextmanager
def user_context(user_id: str) -> Iterator[None]:
    token = _current_user.set(user_id)
    try:
        yield
    finally:
        _current_user.reset(token)


__all__ = [
    "clear_current_user",
    "current_user_id",
    "set_current_user",
    "user_context",
]
