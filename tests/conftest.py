"""Shared pytest fixtures: an isolated account and a Flask client."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

# Configure the runtime before importing any JobPilot module. Settings are cached
# on first use, so this must happen at conftest import time.
_TMP = Path(tempfile.mkdtemp(prefix="jobpilot-tests-"))
os.environ.update(
    JOBPILOT_DATA_DIR=str(_TMP / "data"),
    JOBPILOT_DB_PATH=str(_TMP / "data" / "jobpilot.db"),
    JOBPILOT_AUTH_DB_PATH=str(_TMP / "data" / "auth.db"),
    JOBPILOT_TRACES_DIR=str(_TMP / "data" / "traces"),
    JOBPILOT_UPLOADS_DIR=str(_TMP / "data" / "uploads"),
    JOBPILOT_PRELOAD_MCP="false",
    JOBPILOT_EMBEDDING_PROVIDER="hashing",
    JOBPILOT_ENV="test",
)
os.environ.pop("OPENAI_API_KEY", None)

from jobpilot.bootstrap import bootstrap  # noqa: E402
from jobpilot.core.auth import AuthStore  # noqa: E402
from jobpilot.core.config import get_settings, user_root  # noqa: E402
from jobpilot.core.db import forget_database, get_db  # noqa: E402
from jobpilot.core.user_context import clear_current_user, set_current_user  # noqa: E402

TEST_PASSWORD = "jobpilot-test-password"

_TABLES = [
    "llm_usage",
    "llm_cache",
    "approvals",
    "application_events",
    "applications",
    "job_scores",
    "vectors",
    "memories",
    "chat_messages",
    "chat_sessions",
    "jobs",
    "source_health",
    "skills",
    "projects",
    "education",
    "experiences",
    "profile",
    "kv",
]


def _purge_accounts() -> None:
    auth = AuthStore()
    for user in auth.list_users():
        forget_database(user.id)
        shutil.rmtree(user_root(user.id), ignore_errors=True)
        auth.delete_user(user.id)
    forget_database("")


@pytest.fixture
def account():
    """Create one throw-away account and make it the active user."""
    bootstrap()
    _purge_accounts()
    auth = AuthStore()
    user = auth.register(
        email="tester@example.com",
        password=TEST_PASSWORD,
        display_name="Test User",
    )
    set_current_user(user.id)
    try:
        yield user
    finally:
        clear_current_user()
        _purge_accounts()


@pytest.fixture(autouse=True)
def clean_database(account):
    """Give every test an empty private database."""
    db = get_db()
    db.init()
    for table in _TABLES:
        db.execute(f"DELETE FROM {table}")
    yield db


@pytest.fixture
def seeded_workspace(clean_database):
    """Populate the active workspace with the offline evaluation corpus."""
    from jobpilot.evals.fixtures import seed_eval_workspace

    seed_eval_workspace(clean_database)
    return clean_database


@pytest.fixture
def settings():
    return get_settings()


def login(client, user_id: str) -> None:
    with client.session_transaction() as session:
        session["user_id"] = user_id
        session["_csrf"] = "test-csrf-token"


@pytest.fixture
def client(clean_database, account):
    from jobpilot.ui.app import create_app

    # A dummy key lets the app pass its model-setup guard. Tests never make a
    # network model call: scoring and tailoring stay local and evidence-based.
    settings = get_settings().model_copy(
        update={"openai_api_key": "test-key", "preload_mcp": False}
    )
    app = create_app(settings, preload_mcp=False)
    app.config.update(TESTING=True)
    test_client = app.test_client()
    login(test_client, account.id)
    return test_client
