"""Account storage and password authentication.

Accounts live in a small shared SQLite database. Everything else (profile,
jobs, chats, applications, memories, approvals and model usage) is stored in a
private database per account, under ``data/users/<id>/``.
"""

from __future__ import annotations

import re
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from werkzeug.security import check_password_hash, generate_password_hash

from jobpilot.core.config import get_settings
from jobpilot.core.db import Database
from jobpilot.core.utils import utc_now_iso

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    display_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_login_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
"""


class AuthError(ValueError):
    """A user-facing registration or authentication error."""


@dataclass(frozen=True)
class User:
    id: str
    email: str
    display_name: str
    created_at: str
    last_login_at: str = ""

    @property
    def initials(self) -> str:
        parts = [part for part in self.display_name.split() if part]
        return "".join(part[0] for part in parts[:2]).upper() or "JP"


class AuthStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else get_settings().auth_db_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    # ------------------------------------------------------------- connections
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    def ensure_schema(self) -> None:
        with self._lock, self._session() as conn:
            conn.executescript(_SCHEMA)

    # -------------------------------------------------------------------- read
    def get(self, user_id: str) -> User | None:
        if not user_id:
            return None
        with self._session() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return self._row(row) if row else None

    def get_by_email(self, email: str) -> User | None:
        with self._session() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE email = ? COLLATE NOCASE", (email.strip(),)
            ).fetchone()
        return self._row(row) if row else None

    def count(self) -> int:
        with self._session() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()
        return int(row["n"]) if row else 0

    def list_users(self) -> list[User]:
        with self._session() as conn:
            rows = conn.execute("SELECT * FROM users ORDER BY created_at ASC").fetchall()
        return [self._row(row) for row in rows]

    # ------------------------------------------------------------------- write
    def register(self, *, email: str, password: str, display_name: str) -> User:
        email = email.strip().lower()
        display_name = display_name.strip()
        if not _EMAIL_RE.fullmatch(email):
            raise AuthError("Enter a valid email address.")
        if len(password) < 8:
            raise AuthError("Use at least 8 characters for the password.")
        if len(display_name) < 2:
            raise AuthError("Enter your name.")
        if len(display_name) > 80:
            raise AuthError("That name is too long.")

        user_id = f"usr_{uuid.uuid4().hex}"
        now = utc_now_iso()
        try:
            with self._lock, self._session() as conn:
                conn.execute(
                    "INSERT INTO users(id, email, password_hash, display_name, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        user_id,
                        email,
                        generate_password_hash(password),
                        display_name,
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise AuthError("An account with that email already exists.") from exc

        # Create the account's private SQLite database before returning. If the
        # workspace cannot be created, do not leave a half-registered account.
        try:
            from jobpilot.core.config import user_settings

            Database(user_settings(user_id).db_path).init()
        except Exception:
            self.delete_user(user_id)
            raise

        user = self.get(user_id)
        if user is None:  # pragma: no cover - only possible if the row was removed
            raise AuthError("Could not create the account.")
        return user

    def authenticate(self, email: str, password: str) -> User | None:
        with self._session() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE email = ? COLLATE NOCASE", (email.strip(),)
            ).fetchone()
            if row is None or not check_password_hash(row["password_hash"], password):
                return None
            now = utc_now_iso()
            conn.execute(
                "UPDATE users SET last_login_at = ?, updated_at = ? WHERE id = ?",
                (now, now, row["id"]),
            )
        return self._row(row, last_login_at=now)

    def delete_user(self, user_id: str) -> None:
        """Remove an account row. Workspace files are removed by the caller."""
        with self._lock, self._session() as conn:
            conn.execute("DELETE FROM users WHERE id = ?", (user_id,))

    @staticmethod
    def _row(row: Any, *, last_login_at: str | None = None) -> User:
        return User(
            id=row["id"],
            email=row["email"],
            display_name=row["display_name"],
            created_at=row["created_at"],
            last_login_at=last_login_at if last_login_at is not None else (row["last_login_at"] or ""),
        )


__all__ = ["AuthError", "AuthStore", "User"]
