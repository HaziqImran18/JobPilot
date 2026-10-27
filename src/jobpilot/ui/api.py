"""JSON and SSE API used by the Flask frontend."""

from __future__ import annotations

import json
import queue
import threading
from pathlib import Path
from typing import Any

from flask import (
    Blueprint,
    Response,
    current_app,
    g,
    jsonify,
    request,
    send_file,
    send_from_directory,
    stream_with_context,
)
from pydantic import BaseModel, Field, ValidationError
from werkzeug.exceptions import HTTPException

from jobpilot import __version__
from jobpilot.bootstrap import bootstrap
from jobpilot.core.config import get_settings, user_settings
from jobpilot.core.db import get_db
from jobpilot.core.models import (
    ApplicationStatus,
    JobQuery,
    MemoryKind,
    MemoryStatus,
    Preferences,
    Profile,
)
from jobpilot.core.user_context import user_context
from jobpilot.harness.loop import AgentRunner
from jobpilot.harness.permissions import get_broker
from jobpilot.harness.tracing import TraceStore
from jobpilot.memory.store import MemoryStore
from jobpilot.services.chat import ChatService
from jobpilot.services.discovery import get_discovery
from jobpilot.services.doctor import get_doctor
from jobpilot.services.jobs import get_jobs
from jobpilot.services.matching import get_matching
from jobpilot.services.profile import ProfileService
from jobpilot.services.resume import ResumeError, get_resumes
from jobpilot.services.tailoring import get_tailoring
from jobpilot.services.tracking import get_tracking

api = Blueprint("api", __name__, url_prefix="/api")
bootstrap()


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _body(model: type[BaseModel]) -> Any:
    payload = request.get_json(silent=True) or {}
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise ApiError(f"Invalid request: {exc.errors()[0].get('msg', 'bad payload')}") from exc


def _profile_version() -> str:
    return ProfileService().get().version()


# --------------------------------------------------------------------------- app
@api.get("/health")
def health() -> Response:
    return jsonify({"ok": True, "version": __version__, "mode": get_settings().mode})


@api.get("/bootstrap")
def app_bootstrap() -> Response:
    settings = get_settings()
    profile = ProfileService().get()
    chat = ChatService()
    display_name = profile.full_name or g.user.display_name
    initials = "".join(part[0] for part in display_name.split()[:2]).upper() or "JP"
    return jsonify(
        {
            "version": __version__,
            "mode": settings.mode,
            "account": {
                "id": g.user.id,
                "name": g.user.display_name,
                "email": g.user.email,
            },
            "profile": {
                "name": display_name,
                "headline": profile.headline,
                "initials": initials,
                "location": profile.location,
            },
            "settings": settings.public_dict(),
            "budget": get_matching().llm.budget_status(),
            "counts": get_db().stats(),
            "sessions": chat.list_sessions(20),
            "sources": get_jobs().source_counts(),
        }
    )


# ------------------------------------------------------------------------- chat
class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    session_id: str = ""


@api.post("/chat")
def chat_stream() -> Response:
    payload = _body(ChatRequest)
    message = payload.message.strip()
    if not message:
        raise ApiError("Message cannot be empty.")
    user_id = g.user.id

    def event_stream():
        events: queue.Queue[tuple[str, Any]] = queue.Queue()

        def produce() -> None:
            # New threads start with a clean context; make the account explicit
            # so every tool and persistence call uses this user's workspace.
            with user_context(user_id):
                try:
                    runner = AgentRunner(user_id=user_id)
                    for event in runner.stream(payload.session_id, message):
                        events.put(("event", event.model_dump(mode="json")))
                except Exception as exc:  # noqa: BLE001 - delivered to the UI
                    events.put(("event", {"type": "error", "data": {"text": str(exc)}}))
                finally:
                    events.put(("end", None))

        thread = threading.Thread(target=produce, name="jobpilot-agent", daemon=True)
        thread.start()
        while True:
            try:
                kind, event = events.get(timeout=15)
            except queue.Empty:
                yield ": keep-alive\n\n"
                continue
            if kind == "end":
                break
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return Response(
        stream_with_context(event_stream()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@api.get("/sessions")
def list_sessions() -> Response:
    return jsonify({"sessions": ChatService().list_sessions(40)})


@api.post("/sessions")
def create_session() -> Response:
    session = ChatService().create_session()
    return jsonify({"session": session.model_dump(mode="json")}), 201


@api.delete("/sessions/<session_id>")
def delete_session(session_id: str) -> Response:
    chat = ChatService()
    if chat.get_session(session_id) is None:
        raise ApiError("Session not found.", 404)
    chat.delete_session(session_id)
    return jsonify({"ok": True})


@api.get("/sessions/<session_id>/messages")
def session_messages(session_id: str) -> Response:
    chat = ChatService()
    session = chat.get_session(session_id)
    if session is None:
        raise ApiError("Session not found.", 404)
    messages = [message.model_dump(mode="json") for message in chat.get_messages(session_id)]
    return jsonify({"session": session.model_dump(mode="json"), "messages": messages})


class ApprovalDecision(BaseModel):
    approved: bool


@api.post("/approvals/<approval_id>")
def decide_approval(approval_id: str) -> Response:
    payload = _body(ApprovalDecision)
    approval = get_broker().resolve(approval_id, payload.approved)
    if approval is None:
        raise ApiError("Approval not found or already decided.", 404)
    return jsonify({"approval": approval.model_dump(mode="json")})


@api.get("/approvals")
def pending_approvals() -> Response:
    session_id = request.args.get("session_id", "")
    approvals = [item.model_dump(mode="json") for item in get_broker().pending(session_id)]
    return jsonify({"approvals": approvals})


# ------------------------------------------------------------------------- jobs
@api.get("/jobs")
def list_jobs() -> Response:
    remote = request.args.get("remote", "")
    remote_value: bool | None = True if remote == "true" else False if remote == "false" else None
    min_score_raw = request.args.get("min_score", "")
    try:
        min_score = float(min_score_raw) if min_score_raw else None
    except ValueError as exc:
        raise ApiError("min_score must be a number.", 400) from exc
    posted_raw = request.args.get("posted_within_days", "")
    try:
        posted_within_days = int(posted_raw) if posted_raw else None
    except ValueError as exc:
        raise ApiError("posted_within_days must be a number.", 400) from exc
    if posted_within_days is not None:
        posted_within_days = max(1, min(posted_within_days, 90))
    try:
        limit = min(max(int(request.args.get("limit", 24)), 1), 60)
        offset = max(int(request.args.get("offset", 0)), 0)
    except ValueError as exc:
        raise ApiError("limit and offset must be numbers.", 400) from exc

    force = request.args.get("refresh", "") in {"1", "true", "yes"}
    query = JobQuery(
        query=request.args.get("query", ""),
        location=request.args.get("location", ""),
        remote=remote_value,
        limit=limit,
        posted_within_days=posted_within_days,
        category=request.args.get("category", ""),
        workplace_type=request.args.get("workplace_type", ""),
        seniority=request.args.get("seniority", ""),
        employment_type=request.args.get("employment_type", ""),
        sources=[request.args["source"]] if request.args.get("source") else [],
    )
    # Any substantive search is a user request for live data. Plain board loads
    # stay local; a stale cache is refreshed on the next search, not on every
    # page view. The UI can pass live=0 while the user is still typing, so
    # keystrokes filter the stored snapshot instead of hitting every source.
    live_param = request.args.get("live", "")
    wants_live = live_param != "0" and (
        force
        or any(
            [
                query.query.strip(),
                query.location.strip(),
                query.category,
                query.workplace_type,
                query.seniority,
                query.employment_type,
                query.posted_within_days,
            ]
        )
    )
    discovery = get_discovery()
    if wants_live:
        result = discovery.discover(
            query,
            force=force,
            profile_version=_profile_version(),
            sort=request.args.get("sort", "relevance"),
            min_score=min_score,
            limit=limit,
            offset=offset,
        )
        payload = result.as_dict()
    else:
        page = get_jobs().list_cards(
            profile_version=_profile_version(),
            query=query.query,
            location=query.location,
            remote=query.remote,
            category=query.category,
            workplace_type=query.workplace_type,
            seniority=query.seniority,
            employment_type=query.employment_type,
            posted_within_days=query.posted_within_days,
            sources=query.sources,
            sort=request.args.get("sort", "relevance"),
            min_score=min_score,
            limit=limit,
            offset=offset,
        )
        status = discovery.status()
        payload = {
            "items": page["items"],
            "total": page["total"],
            "limit": limit,
            "offset": offset,
            "exact": page.get("exact", 0),
            "related": page.get("related", 0),
            "sources": page.get("sources") or {},
            "interpreted": page.get("interpreted") or {},
            "source_status": status["sources"],
            "errors": {},
            "cached": True,
            "ok": True,
            "fetched": 0,
            "stored": 0,
            "duration_ms": 0,
            "retrieved_at": None,
            "message": "",
            "stats": status["stats"],
        }
    payload.setdefault("stats", get_jobs().stats())
    return jsonify(payload)


@api.get("/jobs/stats")
def job_stats() -> Response:
    status = get_discovery().status()
    return jsonify(status)


@api.get("/jobs/<job_id>")
def job_detail(job_id: str) -> Response:
    detail = get_jobs().detail(job_id, _profile_version())
    if detail is None:
        raise ApiError("Job not found.", 404)
    return jsonify({"job": detail})


class JobSearchRequest(BaseModel):
    query: str = ""
    location: str = ""
    remote: bool | None = None
    limit: int = Field(default=24, ge=1, le=50)
    offset: int = Field(default=0, ge=0)
    posted_within_days: int | None = Field(default=None, ge=1, le=90)
    sources: list[str] = Field(default_factory=list)
    category: str = ""
    workplace_type: str = ""
    seniority: str = ""
    employment_type: str = ""
    include_related: bool = True
    sort: str = "relevance"
    use_llm: bool = True


@api.post("/jobs/refresh")
def refresh_jobs() -> Response:
    payload = _body(JobSearchRequest)
    settings = get_settings()
    query = JobQuery(
        query=payload.query,
        location=payload.location,
        remote=payload.remote,
        limit=min(payload.limit, 50),
        posted_within_days=payload.posted_within_days,
        sources=payload.sources,
        category=payload.category,
        workplace_type=payload.workplace_type,
        seniority=payload.seniority,
        employment_type=payload.employment_type,
        include_related=payload.include_related,
    )
    result = get_discovery().discover(
        query,
        force=True,
        profile_version=_profile_version(),
        sort=payload.sort,
        limit=min(payload.limit, 50),
        offset=payload.offset,
    )
    ranked = get_matching().rank_jobs(
        query,
        limit=min(payload.limit, 30),
        refresh=False,
        use_llm=settings.is_live and payload.use_llm,
    )
    return jsonify(
        {
            "report": {
                "fetched": result.fetched,
                "stored": result.stored,
                "errors": result.errors,
                "duration_ms": result.duration_ms,
                "interpreted": result.interpreted,
                "retrieved_at": result.retrieved_at,
                "source_status": result.source_status,
            },
            "jobs": result.as_dict(),
            "ranked": [item.model_dump(mode="json") for item in ranked],
            "count": len(ranked),
            "ok": result.ok,
            "message": result.message,
        }
    )


@api.post("/jobs/rank")
def rank_jobs() -> Response:
    payload = _body(JobSearchRequest)
    settings = get_settings()
    query = JobQuery(
        query=payload.query,
        location=payload.location,
        remote=payload.remote,
        limit=min(payload.limit, 50),
        posted_within_days=payload.posted_within_days,
        sources=payload.sources,
        category=payload.category,
        workplace_type=payload.workplace_type,
        seniority=payload.seniority,
        employment_type=payload.employment_type,
        include_related=payload.include_related,
    )
    ranked = get_matching().rank_jobs(
        query,
        limit=min(payload.limit, 30),
        refresh=False,
        use_llm=settings.is_live and payload.use_llm,
    )
    return jsonify(
        {"ranked": [item.model_dump(mode="json") for item in ranked], "count": len(ranked)}
    )


class ImportJobRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2000)


@api.post("/jobs/import")
def import_job() -> Response:
    payload = _body(ImportJobRequest)
    job = get_jobs().add_from_url(payload.url)
    detail = get_jobs().detail(job.id, _profile_version())
    return jsonify({"job": detail}), 201


class ScoreRequest(BaseModel):
    force: bool = False
    use_llm: bool = True


@api.post("/jobs/<job_id>/score")
def score_job(job_id: str) -> Response:
    payload = _body(ScoreRequest)
    settings = get_settings()
    match = get_matching().score_job(
        job_id, force=payload.force, use_llm=settings.is_live and payload.use_llm
    )
    return jsonify({"match": match.model_dump(mode="json")})


class TailorRequest(BaseModel):
    use_llm: bool = True


@api.post("/jobs/<job_id>/tailor")
def tailor_job(job_id: str) -> Response:
    payload = _body(TailorRequest)
    settings = get_settings()
    result = get_tailoring().tailor_cv(job_id, use_llm=settings.is_live and payload.use_llm)
    return jsonify({"tailoring": result.model_dump(mode="json")})


class TrackRequest(BaseModel):
    cv_path: str = ""


@api.post("/jobs/<job_id>/track")
def track_job(job_id: str) -> Response:
    payload = _body(TrackRequest)
    application = get_tracking().add(job_id=job_id, status=ApplicationStatus.saved)
    if payload.cv_path:
        application = get_tracking().attach_cv(application.id, payload.cv_path)
    return jsonify({"application": application.model_dump(mode="json")}), 201


# ------------------------------------------------------------------ applications
@api.get("/applications")
def list_applications() -> Response:
    status_value = request.args.get("status", "")
    application_status = ApplicationStatus(status_value) if status_value else None
    tracking = get_tracking()
    applications = [
        application.model_dump(mode="json")
        for application in tracking.list(status=application_status, limit=300)
    ]
    return jsonify(
        {
            "applications": applications,
            "stats": tracking.stats(),
            "board": tracking.board(),
            "followups": [item.model_dump(mode="json") for item in tracking.followups(7)],
        }
    )


class ApplicationCreate(BaseModel):
    job_id: str = ""
    job_title: str = ""
    company: str = ""
    url: str = ""
    status: ApplicationStatus = ApplicationStatus.saved
    notes: str = ""
    next_action_at: str | None = None


@api.get("/applications/<application_id>")
def get_application(application_id: str) -> Response:
    application = get_tracking().get(application_id)
    if application is None:
        raise ApiError("Application not found.", 404)
    return jsonify({"application": application.model_dump(mode="json")})


@api.post("/applications")
def create_application() -> Response:
    payload = _body(ApplicationCreate)
    application = get_tracking().add(**payload.model_dump())
    return jsonify({"application": application.model_dump(mode="json")}), 201


class ApplicationUpdate(BaseModel):
    status: ApplicationStatus | None = None
    notes: str | None = None
    next_action_at: str | None = None


@api.patch("/applications/<application_id>")
def update_application(application_id: str) -> Response:
    payload = _body(ApplicationUpdate)
    tracking = get_tracking()
    application = tracking.get(application_id)
    if application is None:
        raise ApiError("Application not found.", 404)
    if payload.status is not None:
        application = tracking.update_status(
            application_id, payload.status, "Status updated from the board."
        )
    if payload.notes is not None:
        application = tracking.set_notes(application_id, payload.notes)
    if payload.next_action_at is not None:
        get_db().execute(
            "UPDATE applications SET next_action_at = ?, updated_at = ? WHERE id = ?",
            (payload.next_action_at or None, application.updated_at, application_id),
        )
        application = tracking.get(application_id) or application
    return jsonify({"application": application.model_dump(mode="json")})


@api.delete("/applications/<application_id>")
def delete_application(application_id: str) -> Response:
    if not get_tracking().delete(application_id):
        raise ApiError("Application not found.", 404)
    return jsonify({"ok": True})


# ----------------------------------------------------------------------- memory
@api.get("/memories")
def list_memories() -> Response:
    status_raw = request.args.get("status", "active")
    kind_raw = request.args.get("kind", "")
    status_value = MemoryStatus(status_raw) if status_raw and status_raw != "all" else None
    kind_value = MemoryKind(kind_raw) if kind_raw in {"semantic", "episodic"} else None
    store = MemoryStore()
    return jsonify(
        {
            "memories": [
                memory.model_dump(mode="json")
                for memory in store.list(status=status_value, kind=kind_value, limit=200)
            ],
            "counts": store.count(),
        }
    )


class MemoryCreate(BaseModel):
    text: str = Field(min_length=2, max_length=2000)
    kind: MemoryKind = MemoryKind.semantic
    key: str = ""
    value: str = ""
    importance: float = Field(default=0.7, ge=0, le=1)
    confidence: float = Field(default=0.9, ge=0, le=1)
    explicit: bool = True


@api.post("/memories")
def create_memory() -> Response:
    payload = _body(MemoryCreate)
    memory, action = MemoryStore().add(
        payload.text,
        kind=payload.kind,
        key=payload.key,
        value=payload.value,
        importance=payload.importance,
        confidence=payload.confidence,
        provenance={"source": "ui", "explicit": payload.explicit},
        status=MemoryStatus.active,
    )
    return jsonify({"memory": memory.model_dump(mode="json"), "action": action}), 201


@api.post("/memories/<memory_id>/confirm")
def confirm_memory(memory_id: str) -> Response:
    memory = MemoryStore().confirm(memory_id)
    if memory is None:
        raise ApiError("Memory not found.", 404)
    return jsonify({"memory": memory.model_dump(mode="json")})


@api.delete("/memories/<memory_id>")
def forget_memory(memory_id: str) -> Response:
    if not MemoryStore().forget(memory_id):
        raise ApiError("Memory not found.", 404)
    return jsonify({"ok": True})


@api.get("/memories/recall")
def recall_memories() -> Response:
    query = request.args.get("query", "").strip()
    if not query:
        raise ApiError("Provide a query.")
    memories = MemoryStore().recall(query, k=8)
    return jsonify({"memories": [memory.model_dump(mode="json") for memory in memories]})


# ---------------------------------------------------------------------- profile
@api.get("/profile")
def get_profile() -> Response:
    profile = ProfileService().get()
    return jsonify({"profile": profile.model_dump(mode="json"), "version": profile.version()})


@api.put("/profile")
def update_profile() -> Response:
    payload = request.get_json(silent=True) or {}
    try:
        profile = Profile.model_validate(payload)
    except ValidationError as exc:
        raise ApiError(f"Invalid profile: {exc.errors()[0].get('msg', 'bad payload')}") from exc
    if not isinstance(profile.preferences, Preferences):
        profile.preferences = Preferences()
    saved = ProfileService().save(profile)
    return jsonify({"profile": saved.model_dump(mode="json"), "version": saved.version()})


# ---------------------------------------------------------------------- resumes
@api.get("/resumes")
def list_resumes() -> Response:
    service = get_resumes()
    active = service.active()
    return jsonify(
        {
            "resumes": [resume.summary() for resume in service.list()],
            "active_id": active.id if active else "",
        }
    )


@api.post("/resumes")
def upload_resume() -> Response:
    uploaded = request.files.get("file")
    if uploaded is None or not uploaded.filename:
        raise ApiError("Choose a PDF, DOCX or TXT resume to upload.")
    data = uploaded.read()
    activate = request.form.get("activate", "1") not in {"0", "false", "no"}
    try:
        resume = get_resumes().upload(
            uploaded.filename,
            data,
            mime_type=uploaded.mimetype or "",
            activate=activate,
        )
    except ResumeError as exc:
        raise ApiError(str(exc)) from exc
    status = 201 if resume.status != "error" else 202
    return jsonify({"resume": resume.summary(), "text": resume.text[:12000]}), status


@api.get("/resumes/<resume_id>")
def resume_detail(resume_id: str) -> Response:
    resume = get_resumes().get(resume_id)
    if resume is None:
        raise ApiError("Resume not found.", 404)
    payload = resume.summary()
    payload["text"] = resume.text[:20000]
    return jsonify({"resume": payload})


@api.get("/resumes/<resume_id>/file")
def resume_file(resume_id: str) -> Response:
    service = get_resumes()
    resume = service.get(resume_id)
    path = service.file_path(resume_id)
    if resume is None or path is None:
        raise ApiError("Resume file not found.", 404)
    return send_file(
        path,
        mimetype=resume.mime_type or "application/octet-stream",
        as_attachment=False,
        download_name=resume.filename,
        max_age=0,
    )


@api.post("/resumes/<resume_id>/activate")
def activate_resume(resume_id: str) -> Response:
    try:
        resume = get_resumes().activate(resume_id)
    except ResumeError as exc:
        raise ApiError(str(exc), 404) from exc
    return jsonify({"resume": resume.summary()})


@api.put("/resumes/<resume_id>")
def update_resume(resume_id: str) -> Response:
    payload = request.get_json(silent=True) or {}
    fields = payload.get("fields") if isinstance(payload.get("fields"), dict) else payload
    if not isinstance(fields, dict):
        raise ApiError("Send the reviewed fields as a JSON object.")
    try:
        resume = get_resumes().update_fields(resume_id, fields)
    except ResumeError as exc:
        raise ApiError(str(exc), 404) from exc
    return jsonify({"resume": resume.summary()})


@api.post("/resumes/<resume_id>/apply")
def apply_resume(resume_id: str) -> Response:
    """Copy reviewed resume fields into the profile without erasing other data."""
    service = get_resumes()
    resume = service.get(resume_id)
    if resume is None:
        raise ApiError("Resume not found.", 404)
    payload = request.get_json(silent=True) or {}
    requested = payload.get("fields")
    if not isinstance(requested, list) or not requested:
        requested = list(resume.extracted.keys())

    profile = ProfileService().get()
    source = resume.extracted
    applied: list[str] = []
    updates: dict[str, Any] = {}
    for name in requested:
        if name not in source:
            continue
        value = source[name]
        if name == "skills":
            existing = {skill.name.lower() for skill in profile.skills}
            skills = list(profile.skills)
            for skill_name in value if isinstance(value, list) else []:
                cleaned = str(skill_name).strip()
                if cleaned and cleaned.lower() not in existing:
                    existing.add(cleaned.lower())
                    skills.append({"name": cleaned, "level": 3, "years": 0, "category": "general"})
            if skills:
                updates["skills"] = skills
                applied.append("skills")
            continue
        if name == "links":
            merged = dict(profile.links)
            merged.update({str(k): str(v) for k, v in (value or {}).items() if v})
            updates["links"] = merged
            applied.append("links")
            continue
        if name in {"full_name", "headline", "email", "phone", "location", "summary"}:
            text = str(value or "").strip()
            if text:
                updates[name] = text
                applied.append(name)
            continue
        if name == "years_experience":
            continue

    if not updates:
        raise ApiError("None of the selected fields had a value to apply.")

    merged_profile = profile.model_copy(update={**updates, "updated_at": None})
    if "skills" in updates:
        from jobpilot.core.models import Skill

        merged_profile = merged_profile.model_copy(
            update={"skills": [Skill.model_validate(item) for item in updates["skills"]]}
        )
    saved = ProfileService().save(merged_profile)
    return jsonify(
        {
            "profile": saved.model_dump(mode="json"),
            "version": saved.version(),
            "applied": applied,
        }
    )


@api.delete("/resumes/<resume_id>")
def delete_resume(resume_id: str) -> Response:
    try:
        get_resumes().delete(resume_id)
    except ResumeError as exc:
        raise ApiError(str(exc), 404) from exc
    return jsonify({"ok": True})


# ----------------------------------------------------------------------- system
@api.get("/system/stats")
def system_stats() -> Response:
    return jsonify(get_doctor().system_stats())


@api.get("/system/doctor")
def doctor() -> Response:
    probe = request.args.get("probe", "0") in {"1", "true", "yes"}
    mcp = request.args.get("mcp", "0") in {"1", "true", "yes"}
    report = get_doctor().run(probe_sources=probe, check_mcp=mcp)
    return jsonify(report.model_dump(mode="json"))


@api.get("/system/traces")
def traces() -> Response:
    limit = min(max(int(request.args.get("limit", 30)), 1), 100)
    return jsonify(
        {"traces": [item.model_dump(mode="json") for item in TraceStore().list_runs(limit)]}
    )


@api.get("/system/traces/<run_id>")
def trace_detail(run_id: str) -> Response:
    events = TraceStore().read(run_id)
    if not events:
        raise ApiError("Trace not found.", 404)
    return jsonify({"run_id": run_id, "events": events})


@api.get("/system/usage")
def llm_usage() -> Response:
    return jsonify({"usage": get_matching().llm.recent_usage(80)})


@api.get("/system/mcp")
def mcp_status() -> Response:
    from jobpilot.harness.mcp_client import get_mcp_manager

    manager = get_mcp_manager(g.user.id)
    if request.args.get("start", "1") in {"1", "true", "yes"}:
        manager.start()
    tools = manager.tools()
    return jsonify(
        {
            "servers": manager.servers,
            "errors": manager.errors,
            "tools": [tool.name for tool in tools],
            "count": len(tools),
        }
    )


# ------------------------------------------------------------------------ files
@api.get("/files/<path:filename>")
def download_file(filename: str) -> Response:
    settings = user_settings(g.user.id)
    if filename != Path(filename).name or not filename.endswith(".md"):
        raise ApiError("Invalid file name.", 400)
    path = (settings.uploads_dir / filename).resolve()
    if not path.is_file() or settings.uploads_dir.resolve() not in path.parents:
        raise ApiError("File not found.", 404)
    return send_from_directory(
        settings.uploads_dir, filename, as_attachment=True, download_name=filename
    )


# ---------------------------------------------------------------------- handlers
@api.errorhandler(ApiError)
def handle_api_error(error: ApiError):
    return jsonify({"error": str(error)}), error.status


@api.errorhandler(ValidationError)
def handle_validation_error(error: ValidationError):
    return jsonify(
        {"error": f"Invalid data: {error.errors()[0].get('msg', 'validation failed')}"}
    ), 400


@api.errorhandler(HTTPException)
def handle_http_error(error: HTTPException):
    return jsonify({"error": error.description, "status": error.code}), error.code or 500


@api.errorhandler(Exception)
def handle_unexpected(error: Exception):
    current_app.logger.exception("Unhandled API error")
    return jsonify({"error": f"Server error: {error}"}), 500
