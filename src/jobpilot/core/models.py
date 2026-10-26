"""Typed domain models shared by the harness, servers and UI."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from jobpilot.core.utils import json_dumps, to_iso, utc_now


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


# --------------------------------------------------------------------------- jobs
class JobQuery(StrictModel):
    query: str = ""
    location: str = ""
    remote: bool | None = None
    limit: int = Field(default=20, ge=1, le=100)
    sources: list[str] = Field(default_factory=list)
    posted_within_days: int | None = Field(default=None, ge=1, le=90)


class Salary(StrictModel):
    minimum: float | None = None
    maximum: float | None = None
    currency: str = "USD"
    period: str = "year"


class RawJob(StrictModel):
    source: str
    external_id: str
    title: str
    company: str
    url: str
    description: str = ""
    location: str = ""
    remote: bool | None = None
    employment_type: str = ""
    salary: Salary | None = None
    posted_at: Any = None
    tags: list[str] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)


class Job(StrictModel):
    id: str
    source: str
    external_id: str
    title: str
    company: str
    url: str
    description: str = ""
    location: str = ""
    remote: bool | None = None
    employment_type: str = ""
    salary: Salary | None = None
    posted_at: Any = None
    fetched_at: str = Field(default_factory=lambda: utc_now().isoformat(timespec="seconds"))
    fingerprint: str = ""
    tags: list[str] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)
    score: float | None = None

    @property
    def posted_iso(self) -> str | None:
        return to_iso(self.posted_at)


class JobCard(StrictModel):
    """Compact representation shown to the agent and list views."""

    id: str
    title: str
    company: str
    location: str
    remote: bool | None = None
    url: str
    source: str
    salary: str | None = None
    posted_at: str | None = None
    top_skills: list[str] = Field(default_factory=list)
    score: float | None = None


# ------------------------------------------------------------------------ profile
class Experience(StrictModel):
    id: str = ""
    company: str
    title: str
    start_date: str = ""
    end_date: str = ""
    location: str = ""
    bullets: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    is_current: bool = False


class Education(StrictModel):
    id: str = ""
    institution: str
    degree: str = ""
    field_of_study: str = ""
    start_date: str = ""
    end_date: str = ""
    details: str = ""


class Project(StrictModel):
    id: str = ""
    name: str
    description: str = ""
    url: str = ""
    skills: list[str] = Field(default_factory=list)


class Skill(StrictModel):
    id: str = ""
    name: str
    level: int = Field(default=3, ge=1, le=5)
    years: float = 0
    category: str = "general"


class Preferences(StrictModel):
    target_roles: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    remote_preferred: bool = True
    onsite_ok: bool = True
    salary_floor: float | None = None
    currency: str = "PKR"
    salary_period: str = "month"
    seniority: str = "entry"
    excluded_companies: list[str] = Field(default_factory=list)
    excluded_keywords: list[str] = Field(default_factory=list)
    skills_to_avoid: list[str] = Field(default_factory=list)
    work_authorization: str = ""
    notes: str = ""


class Profile(StrictModel):
    id: int = 1
    full_name: str = ""
    headline: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    links: dict[str, str] = Field(default_factory=dict)
    summary: str = ""
    preferences: Preferences = Field(default_factory=Preferences)
    experiences: list[Experience] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    skills: list[Skill] = Field(default_factory=list)
    updated_at: str | None = None

    def fact_corpus(self) -> list[str]:
        """Every verifiable statement the CV writer is allowed to draw from."""
        facts: list[str] = [self.headline, self.summary, self.location]
        facts.extend(f"{s.name} (level {s.level}/5, {s.years:g} years)" for s in self.skills)
        for exp in self.experiences:
            facts.append(
                f"{exp.title} at {exp.company} ({exp.start_date} - {exp.end_date or 'present'})"
            )
            facts.extend(exp.bullets)
            facts.extend(exp.skills)
        for edu in self.education:
            facts.append(f"{edu.degree} {edu.field_of_study} at {edu.institution} ({edu.end_date})")
        for project in self.projects:
            facts.append(f"{project.name}: {project.description}")
            facts.extend(project.skills)
        return [f for f in facts if f and f.strip()]

    def version(self) -> str:
        import hashlib

        payload = json_dumps(self.model_dump(mode="json", exclude={"updated_at"}))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


# ------------------------------------------------------------------------- match
class SkillGap(StrictModel):
    skill: str
    importance: Literal["high", "medium", "low"] = "medium"
    have: bool = False
    note: str = ""


class MatchResult(StrictModel):
    job_id: str
    score: float = Field(ge=0, le=100)
    breakdown: dict[str, float] = Field(default_factory=dict)
    matched_skills: list[str] = Field(default_factory=list)
    missing_skills: list[str] = Field(default_factory=list)
    rationale: str = ""
    model: str = "heuristic"
    cached: bool = False
    created_at: str = Field(default_factory=lambda: utc_now().isoformat(timespec="seconds"))


class RankedJob(StrictModel):
    job: JobCard
    match: MatchResult


# ------------------------------------------------------------------------ memory
class MemoryKind(StrEnum):
    semantic = "semantic"
    episodic = "episodic"


class MemoryStatus(StrEnum):
    active = "active"
    needs_confirmation = "needs_confirmation"
    merged = "merged"
    forgotten = "forgotten"


class Memory(StrictModel):
    id: str
    kind: MemoryKind = MemoryKind.semantic
    text: str
    key: str = ""
    value: str = ""
    importance: float = Field(default=0.5, ge=0, le=1)
    confidence: float = Field(default=0.8, ge=0, le=1)
    provenance: dict[str, Any] = Field(default_factory=dict)
    status: MemoryStatus = MemoryStatus.active
    created_at: str = ""
    updated_at: str = ""
    last_used_at: str | None = None
    use_count: int = 0
    similarity: float | None = None


class MemoryProposal(StrictModel):
    text: str
    kind: MemoryKind = MemoryKind.semantic
    key: str = ""
    value: str = ""
    importance: float = 0.5
    confidence: float = 0.8
    requires_confirmation: bool = False
    reason: str = ""


# ------------------------------------------------------------------- applications
class ApplicationStatus(StrEnum):
    saved = "saved"
    tailoring = "tailoring"
    ready = "ready"
    applied = "applied"
    interviewing = "interviewing"
    offer = "offer"
    rejected = "rejected"
    withdrawn = "withdrawn"


class ApplicationEvent(StrictModel):
    id: str
    application_id: str
    status: str
    note: str = ""
    created_at: str


class Application(StrictModel):
    id: str
    job_id: str | None = None
    job_title: str = ""
    company: str = ""
    url: str = ""
    status: ApplicationStatus = ApplicationStatus.saved
    notes: str = ""
    cv_path: str = ""
    cover_letter_path: str = ""
    next_action_at: str | None = None
    created_at: str = ""
    updated_at: str = ""
    events: list[ApplicationEvent] = Field(default_factory=list)


# ---------------------------------------------------------------------- agent I/O
class PermissionTier(StrEnum):
    read = "read"
    write_local = "write_local"
    external = "external"


class ToolSpec(StrictModel):
    name: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    tier: PermissionTier = PermissionTier.read
    server: str = ""


class ToolCall(StrictModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResult(StrictModel):
    call_id: str
    name: str
    ok: bool
    content: str = ""
    data: Any = None
    error: str = ""
    duration_ms: int = 0
    tier: PermissionTier = PermissionTier.read


class ApprovalRequest(StrictModel):
    id: str
    run_id: str
    session_id: str = ""
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""
    status: Literal["pending", "approved", "denied", "expired"] = "pending"
    created_at: str = ""
    decided_at: str | None = None


class AgentEvent(StrictModel):
    type: Literal[
        "status",
        "token",
        "tool_call",
        "tool_result",
        "approval_required",
        "memory_proposal",
        "message",
        "error",
        "done",
    ]
    data: dict[str, Any] = Field(default_factory=dict)


# ------------------------------------------------------------------------ tailoring
class ClaimCheck(StrictModel):
    text: str
    supported: bool
    evidence: list[str] = Field(default_factory=list)
    confidence: float = 0.0


class TailorResult(StrictModel):
    job_id: str
    job_title: str = ""
    company: str = ""
    headline: str = ""
    summary: str = ""
    bullets: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    cover_letter: str = ""
    claims: list[ClaimCheck] = Field(default_factory=list)
    unsupported: list[str] = Field(default_factory=list)
    verified: bool = False
    model: str = "deterministic"
    cv_path: str = ""
    generated_at: str = Field(default_factory=lambda: utc_now().isoformat(timespec="seconds"))


# -------------------------------------------------------------------------- system
class DoctorCheck(StrictModel):
    name: str
    ok: bool
    detail: str = ""
    latency_ms: int | None = None
    group: str = "system"


class DoctorReport(StrictModel):
    mode: str
    version: str
    checks: list[DoctorCheck] = Field(default_factory=list)
    generated_at: str = Field(default_factory=lambda: utc_now().isoformat(timespec="seconds"))


class BudgetStatus(StrictModel):
    session_spent: float = 0.0
    daily_spent: float = 0.0
    session_cap: float = 0.0
    daily_cap: float = 0.0
    tokens_today: int = 0
    calls_today: int = 0


class TraceSummary(StrictModel):
    run_id: str
    started_at: str
    ended_at: str = ""
    steps: int = 0
    tool_calls: int = 0
    cost_usd: float = 0.0
    tokens: int = 0
    status: str = "running"
    session_id: str = ""


# --------------------------------------------------------------------------- chat
class ChatMessage(StrictModel):
    id: str
    session_id: str
    role: Literal["user", "assistant", "system", "tool"]
    content: str
    meta: dict[str, Any] = Field(default_factory=dict)
    created_at: str = ""


class ChatSession(StrictModel):
    id: str
    title: str = "New conversation"
    created_at: str = ""
    updated_at: str = ""
    rolling_summary: str = ""
