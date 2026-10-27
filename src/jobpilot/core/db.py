"""SQLite persistence layer with versioned, idempotent migrations."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from jobpilot.core.config import get_settings, user_settings
from jobpilot.core.user_context import current_user_id
from jobpilot.core.utils import json_loads, utc_now_iso

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS profile (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    full_name TEXT NOT NULL DEFAULT '',
    headline TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    location TEXT NOT NULL DEFAULT '',
    links TEXT NOT NULL DEFAULT '{}',
    summary TEXT NOT NULL DEFAULT '',
    preferences TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS experiences (
    id TEXT PRIMARY KEY,
    company TEXT NOT NULL,
    title TEXT NOT NULL,
    start_date TEXT NOT NULL DEFAULT '',
    end_date TEXT NOT NULL DEFAULT '',
    location TEXT NOT NULL DEFAULT '',
    bullets TEXT NOT NULL DEFAULT '[]',
    skills TEXT NOT NULL DEFAULT '[]',
    is_current INTEGER NOT NULL DEFAULT 0,
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS education (
    id TEXT PRIMARY KEY,
    institution TEXT NOT NULL,
    degree TEXT NOT NULL DEFAULT '',
    field_of_study TEXT NOT NULL DEFAULT '',
    start_date TEXT NOT NULL DEFAULT '',
    end_date TEXT NOT NULL DEFAULT '',
    details TEXT NOT NULL DEFAULT '',
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    skills TEXT NOT NULL DEFAULT '[]',
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS skills (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    level INTEGER NOT NULL DEFAULT 3,
    years REAL NOT NULL DEFAULT 0,
    category TEXT NOT NULL DEFAULT 'general'
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    title TEXT NOT NULL,
    company TEXT NOT NULL,
    url TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    location TEXT NOT NULL DEFAULT '',
    remote INTEGER,
    employment_type TEXT NOT NULL DEFAULT '',
    salary_min REAL,
    salary_max REAL,
    salary_currency TEXT,
    salary_period TEXT,
    posted_at TEXT,
    fetched_at TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '[]',
    raw TEXT NOT NULL DEFAULT '{}',
    category TEXT NOT NULL DEFAULT 'other',
    seniority TEXT NOT NULL DEFAULT '',
    workplace_type TEXT NOT NULL DEFAULT '',
    company_location TEXT NOT NULL DEFAULT '',
    job_location TEXT NOT NULL DEFAULT '',
    eligible_locations TEXT NOT NULL DEFAULT '[]',
    remote_scope TEXT NOT NULL DEFAULT 'unknown',
    location_confidence TEXT NOT NULL DEFAULT 'unknown',
    status TEXT NOT NULL DEFAULT 'active',
    first_seen_at TEXT,
    last_seen_at TEXT,
    last_checked_at TEXT,
    UNIQUE (source, external_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_fingerprint ON jobs(fingerprint);
CREATE INDEX IF NOT EXISTS idx_jobs_posted ON jobs(posted_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company);

CREATE VIRTUAL TABLE IF NOT EXISTS jobs_fts USING fts5(
    title, company, description, location, tags,
    content='jobs', content_rowid='rowid', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS jobs_ai AFTER INSERT ON jobs BEGIN
    INSERT INTO jobs_fts(rowid, title, company, description, location, tags)
    VALUES (new.rowid, new.title, new.company, new.description, new.location, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS jobs_ad AFTER DELETE ON jobs BEGIN
    INSERT INTO jobs_fts(jobs_fts, rowid, title, company, description, location, tags)
    VALUES ('delete', old.rowid, old.title, old.company, old.description, old.location, old.tags);
END;
CREATE TRIGGER IF NOT EXISTS jobs_au AFTER UPDATE ON jobs BEGIN
    INSERT INTO jobs_fts(jobs_fts, rowid, title, company, description, location, tags)
    VALUES ('delete', old.rowid, old.title, old.company, old.description, old.location, old.tags);
    INSERT INTO jobs_fts(rowid, title, company, description, location, tags)
    VALUES (new.rowid, new.title, new.company, new.description, new.location, new.tags);
END;

CREATE TABLE IF NOT EXISTS job_scores (
    job_id TEXT NOT NULL,
    profile_version TEXT NOT NULL,
    score REAL NOT NULL,
    breakdown TEXT NOT NULL DEFAULT '{}',
    matched_skills TEXT NOT NULL DEFAULT '[]',
    missing_skills TEXT NOT NULL DEFAULT '[]',
    rationale TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT 'heuristic',
    created_at TEXT NOT NULL,
    PRIMARY KEY (job_id, profile_version),
    FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS applications (
    id TEXT PRIMARY KEY,
    job_id TEXT,
    job_title TEXT NOT NULL DEFAULT '',
    company TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'saved',
    notes TEXT NOT NULL DEFAULT '',
    cv_path TEXT NOT NULL DEFAULT '',
    cover_letter_path TEXT NOT NULL DEFAULT '',
    next_action_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (job_id),
    FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_applications_status ON applications(status);

CREATE TABLE IF NOT EXISTS application_events (
    id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL,
    status TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY (application_id) REFERENCES applications(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL DEFAULT 'semantic',
    text TEXT NOT NULL,
    key TEXT NOT NULL DEFAULT '',
    value TEXT NOT NULL DEFAULT '',
    importance REAL NOT NULL DEFAULT 0.5,
    confidence REAL NOT NULL DEFAULT 0.8,
    provenance TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_used_at TEXT,
    use_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_memories_status ON memories(status, kind);

CREATE TABLE IF NOT EXISTS vectors (
    owner_type TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    model TEXT NOT NULL,
    dim INTEGER NOT NULL,
    vector BLOB NOT NULL,
    PRIMARY KEY (owner_type, owner_id, model)
);

CREATE TABLE IF NOT EXISTS chat_sessions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT 'New conversation',
    rolling_summary TEXT NOT NULL DEFAULT '',
    pinned TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    meta TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES chat_sessions(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON chat_messages(session_id, created_at);

CREATE TABLE IF NOT EXISTS llm_cache (
    key TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    response TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cost REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cost REAL NOT NULL DEFAULT 0,
    cached INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_created ON llm_usage(created_at);

CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    tool TEXT NOT NULL,
    arguments TEXT NOT NULL DEFAULT '{}',
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    decided_at TEXT
);

CREATE TABLE IF NOT EXISTS source_health (
    name TEXT PRIMARY KEY,
    ok INTEGER NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    latency_ms INTEGER,
    checked_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resumes (
    id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    mime_type TEXT NOT NULL DEFAULT '',
    size_bytes INTEGER NOT NULL DEFAULT 0,
    sha256 TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'uploaded',
    text TEXT NOT NULL DEFAULT '',
    extracted TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT '',
    is_active INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_resumes_created ON resumes(created_at DESC);

CREATE TABLE IF NOT EXISTS search_runs (
    signature TEXT PRIMARY KEY,
    query TEXT NOT NULL DEFAULT '',
    filters TEXT NOT NULL DEFAULT '{}',
    report TEXT NOT NULL DEFAULT '{}',
    total INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
"""


# Additive columns introduced after the first release. Existing accounts are
# upgraded in place; `CREATE TABLE IF NOT EXISTS` alone would leave them behind.
_JOB_EXTRA_COLUMNS: dict[str, str] = {
    "category": "TEXT NOT NULL DEFAULT 'other'",
    "seniority": "TEXT NOT NULL DEFAULT ''",
    "workplace_type": "TEXT NOT NULL DEFAULT ''",
    "company_location": "TEXT NOT NULL DEFAULT ''",
    "job_location": "TEXT NOT NULL DEFAULT ''",
    "eligible_locations": "TEXT NOT NULL DEFAULT '[]'",
    "remote_scope": "TEXT NOT NULL DEFAULT 'unknown'",
    "location_confidence": "TEXT NOT NULL DEFAULT 'unknown'",
    "status": "TEXT NOT NULL DEFAULT 'active'",
    "first_seen_at": "TEXT",
    "last_seen_at": "TEXT",
    "last_checked_at": "TEXT",
}
_EXTRA_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_jobs_category ON jobs(category)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_workplace ON jobs(workplace_type)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_last_seen ON jobs(last_seen_at DESC)",
)


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply additive migrations idempotently.

    SQLite cannot add a column that already exists, so every column is checked
    against ``PRAGMA table_info`` before it is added. Running this on a brand-new
    database is a no-op because the schema above already contains the columns.
    """
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
    for name, ddl in _JOB_EXTRA_COLUMNS.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {ddl}")  # noqa: S608 - fixed names
    for statement in _EXTRA_INDEXES:
        conn.execute(statement)
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (2, ?)",
        (utc_now_iso(),),
    )


class Database:
    """Small wrapper around sqlite3.

    A fresh connection is opened per unit of work. SQLite is fast enough for
    this workload and this avoids sharing connections across Flask threads or
    MCP server processes.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        settings = get_settings()
        self.path = Path(path) if path else settings.db_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_lock = threading.Lock()
        self._initialized = False

    # ------------------------------------------------------------- connections
    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except Exception:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    # -------------------------------------------------------------- lifecycle
    def init(self) -> None:
        with self._init_lock:
            if self._initialized:
                return
            with self.connect() as conn:
                conn.executescript(_SCHEMA)
                conn.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (1, ?)",
                    (utc_now_iso(),),
                )
                _migrate(conn)
            self._initialized = True

    # ----------------------------------------------------------------- queries
    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        self.init()
        with self.connect() as conn:
            return list(conn.execute(sql, params).fetchall())

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        self.init()
        with self.connect() as conn:
            conn.execute(sql, params)

    def execute_many(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        self.init()
        with self.connect() as conn:
            conn.executemany(sql, rows)

    def get_kv(self, key: str, default: str = "") -> str:
        row = self.query_one("SELECT value FROM kv WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_kv(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO kv(key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, utc_now_iso()),
        )

    def get_json(self, key: str, default: Any = None) -> Any:
        raw = self.get_kv(key)
        return json_loads(raw, default) if raw else default

    def stats(self) -> dict[str, int | float]:
        self.init()
        counts: dict[str, int | float] = {}
        for table in ("jobs", "applications", "memories", "chat_sessions", "llm_usage"):
            row = self.query_one(f"SELECT COUNT(*) AS n FROM {table}")  # noqa: S608 - fixed names
            counts[table] = int(row["n"]) if row else 0
        spend = self.query_one("SELECT COALESCE(SUM(cost), 0) AS total FROM llm_usage")
        counts["spend_usd"] = round(float(spend["total"]), 4) if spend else 0.0
        return counts


_databases: dict[str, Database] = {}
_db_lock = threading.Lock()


def get_db() -> Database:
    """Return the database for the active user.

    Each account has its own SQLite file. That keeps every existing service
    (profile, chat, memory, approvals, scores and usage) isolated without
    threading a user_id through dozens of SQL statements. MCP subprocesses get
    the same path through their environment.
    """
    user_id = current_user_id()
    key = user_id or "__shared__"
    with _db_lock:
        database = _databases.get(key)
        if database is None:
            path = user_settings(user_id).db_path if user_id else get_settings().db_path
            database = Database(path)
            database.init()
            _databases[key] = database
        return database


def forget_database(user_id: str) -> None:
    """Drop a cached handle after an account or test workspace is removed."""
    with _db_lock:
        _databases.pop(user_id or "__shared__", None)
