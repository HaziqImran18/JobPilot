"""Application settings, loaded once from environment variables and `.env`."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from jobpilot.core.user_context import current_user_id

PROJECT_ROOT = Path(__file__).resolve().parents[3]

_USER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,64}$")


class Settings(BaseSettings):
    """Runtime configuration.

    Every value can be overridden with an environment variable. The core fields
    also accept the conventional (unprefixed) names used by hosting platforms.
    """

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_prefix="JOBPILOT_",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Runtime
    env: str = "development"
    debug: bool = True
    host: str = "127.0.0.1"
    port: int = 5000
    secret_key: str = "jobpilot-dev-secret"
    session_days: int = 14
    allow_registration: bool = True

    # LLM
    # JobPilot targets OpenAI by default. Any OpenAI-compatible endpoint still
    # works by overriding the base URL and model names.
    openai_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("OPENAI_API_KEY", "JOBPILOT_OPENAI_API_KEY"),
    )
    openai_base_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("OPENAI_BASE_URL", "JOBPILOT_OPENAI_BASE_URL"),
    )
    model_fast: str = "gpt-4o-mini"
    model_strong: str = "gpt-4o"
    embedding_model: str = "text-embedding-3-small"
    embedding_provider: str = "auto"  # auto | openai | local | hashing
    embedding_dim: int = 256

    # Harness limits
    max_budget_usd: float = 2.0
    daily_budget_usd: float = 1.0
    max_steps_per_run: int = 12
    max_consecutive_failures: int = 3
    max_context_tokens: int = 12_000
    max_tool_output_chars: int = 6_000
    request_timeout: float = 60.0
    llm_max_retries: int = 3

    # Storage. Per-user data lives under data/users/<id>/; auth.db is shared.
    data_dir: Path = Path("data")
    db_path: Path = Path("data/jobpilot.db")
    auth_db_path: Path = Path("data/auth.db")
    traces_dir: Path = Path("data/traces")
    uploads_dir: Path = Path("data/uploads")

    # Sources (all free, no API key required)
    enable_remotive: bool = True
    enable_arbeitnow: bool = True
    enable_remoteok: bool = True
    enable_rss: bool = True
    enable_jobicy: bool = True
    enable_himalayas: bool = True
    enable_linkedin: bool = True
    enable_mustakbil: bool = True
    enable_talent: bool = True
    enable_indeed: bool = True
    http_timeout: float = 20.0
    user_agent: str = "JobPilot/0.2 (job search agent)"

    # UI defaults
    default_location: str = ""
    default_remote: bool = True
    preload_mcp: bool = False

    # Source scope by workplace type. Narrowing the scope improves result
    # quality: onsite/hybrid roles are sourced from Pakistan (Mustakbil) and
    # Germany (Arbeitnow) only; remote roles are sourced worldwide with a
    # focus on Europe and North America.
    onsite_hybrid_sources: str = "mustakbil,arbeitnow"
    remote_sources: str = "remoteok,remotive,himalayas,jobicy,linkedin,talent"

    @field_validator(
        "data_dir",
        "db_path",
        "auth_db_path",
        "traces_dir",
        "uploads_dir",
        mode="after",
    )
    @classmethod
    def _resolve_path(cls, value: Path) -> Path:
        return value if value.is_absolute() else PROJECT_ROOT / value

    @field_validator("embedding_provider")
    @classmethod
    def _validate_provider(cls, value: str) -> str:
        allowed = {"auto", "openai", "local", "hashing"}
        value = value.strip().lower()
        if value not in allowed:
            raise ValueError(f"embedding_provider must be one of {sorted(allowed)}")
        return value

    @property
    def is_live(self) -> bool:
        """True when a real model can be called."""
        return bool(self.openai_api_key.strip())

    @property
    def mode(self) -> str:
        return "live" if self.is_live else "unconfigured"

    def remote_sources_list(self) -> list[str]:
        return [s.strip() for s in self.remote_sources.split(",") if s.strip()]

    def onsite_hybrid_sources_list(self) -> list[str]:
        return [s.strip() for s in self.onsite_hybrid_sources.split(",") if s.strip()]

    @property
    def resolved_embedding_provider(self) -> str:
        if self.embedding_provider != "auto":
            return self.embedding_provider
        # Auto stays local and free. Set JOBPILOT_EMBEDDING_PROVIDER=openai to
        # use the paid embedding endpoint explicitly.
        return "hashing"

    def ensure_dirs(self) -> None:
        for path in (
            self.data_dir,
            self.db_path.parent,
            self.auth_db_path.parent,
            self.traces_dir,
            self.uploads_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def public_dict(self) -> dict[str, object]:
        """Safe settings for the UI (never includes secrets)."""
        return {
            "mode": self.mode,
            "env": self.env,
            "model_fast": self.model_fast,
            "model_strong": self.model_strong,
            "embedding_provider": self.resolved_embedding_provider,
            "max_budget_usd": self.max_budget_usd,
            "daily_budget_usd": self.daily_budget_usd,
            "max_steps_per_run": self.max_steps_per_run,
            "default_location": self.default_location,
            "default_remote": self.default_remote,
            "preload_mcp": self.preload_mcp,
            "source_scope": {
                "onsite_hybrid": self.onsite_hybrid_sources_list(),
                "remote": self.remote_sources_list(),
            },
            "sources": {
                "remotive": self.enable_remotive,
                "arbeitnow": self.enable_arbeitnow,
                "remoteok": self.enable_remoteok,
                "rss": self.enable_rss,
                "jobicy": self.enable_jobicy,
                "himalayas": self.enable_himalayas,
                "linkedin": self.enable_linkedin,
                "mustakbil": self.enable_mustakbil,
                "talent": self.enable_talent,
                "indeed": self.enable_indeed,
            },
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings


def user_root(user_id: str) -> Path:
    """Return the private workspace directory for one account."""
    if not _USER_ID_RE.fullmatch(user_id or ""):
        raise ValueError("Invalid user id.")
    return get_settings().data_dir / "users" / user_id


def user_settings(user_id: str) -> Settings:
    """Settings with paths redirected into one user's private workspace."""
    if not user_id:
        return get_settings()
    root = user_root(user_id)
    settings = get_settings().model_copy(
        update={
            "data_dir": root,
            "db_path": root / "jobpilot.db",
            "traces_dir": root / "traces",
            "uploads_dir": root / "uploads",
            "preload_mcp": False,
        }
    )
    settings.ensure_dirs()
    return settings


def current_settings() -> Settings:
    """Resolve the active account's settings (or the shared defaults)."""
    user_id = current_user_id()
    return user_settings(user_id) if user_id else get_settings()
