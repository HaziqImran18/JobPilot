"""Page routes for the Flask UI, including account access."""

from __future__ import annotations

import secrets

from flask import (
    Blueprint,
    abort,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from jobpilot import __version__
from jobpilot.bootstrap import bootstrap_workspace
from jobpilot.core.auth import AuthError, AuthStore
from jobpilot.core.config import get_settings
from jobpilot.core.user_context import set_current_user

views = Blueprint("views", __name__)


def csrf_token() -> str:
    token = session.get("_csrf")
    if not token:
        token = secrets.token_urlsafe(24)
        session["_csrf"] = token
    return token


def _csrf_ok() -> bool:
    sent = request.form.get("_csrf", "")
    stored = session.get("_csrf", "")
    return bool(sent and stored and secrets.compare_digest(sent, stored))


def _login(user) -> None:
    session.clear()
    session["user_id"] = user.id
    session["_csrf"] = secrets.token_urlsafe(24)
    session.permanent = True
    set_current_user(user.id)
    bootstrap_workspace(user.id)


def _page(template: str, title: str, subtitle: str, active: str):
    return render_template(
        template,
        page_title=title,
        page_subtitle=subtitle,
        active_page=active,
        app_version=__version__,
        mode=get_settings().mode,
    )


# ------------------------------------------------------------------- accounts
@views.route("/register", methods=["GET", "POST"])
def register_page():
    if g.get("user") is not None:
        return redirect(url_for("views.chat_page"))
    auth = AuthStore()
    if not get_settings().allow_registration and auth.count() > 0:
        return render_template(
            "auth/register.html",
            error="Registration is disabled on this server.",
            first_run=False,
            app_version=__version__,
        ), 403

    error = ""
    if request.method == "POST":
        password = request.form.get("password", "")
        if not _csrf_ok():
            error = "Your session expired. Reload the page and try again."
        elif password != request.form.get("password_confirm", ""):
            error = "The passwords do not match."
        else:
            try:
                user = auth.register(
                    email=request.form.get("email", ""),
                    password=password,
                    display_name=request.form.get("display_name", ""),
                )
            except AuthError as exc:
                error = str(exc)
            else:
                _login(user)
                return redirect(url_for("views.chat_page"))

    return render_template(
        "auth/register.html",
        error=error,
        first_run=auth.count() == 0,
        app_version=__version__,
    )


@views.route("/login", methods=["GET", "POST"])
def login_page():
    if g.get("user") is not None:
        return redirect(url_for("views.chat_page"))
    auth = AuthStore()
    if auth.count() == 0:
        return redirect(url_for("views.register_page"))

    error = ""
    email = ""
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        if not _csrf_ok():
            error = "Your session expired. Reload the page and try again."
        else:
            user = auth.authenticate(email, request.form.get("password", ""))
            if user is None:
                error = "Email or password is incorrect."
            else:
                _login(user)
                return redirect(url_for("views.chat_page"))

    return render_template(
        "auth/login.html",
        error=error,
        email=email,
        app_version=__version__,
    )


@views.post("/logout")
def logout():
    if not _csrf_ok():
        abort(400, description="Invalid sign-out request.")
    session.clear()
    return redirect(url_for("views.login_page"))


# ----------------------------------------------------------------------- pages
@views.get("/setup")
def setup():
    settings = get_settings()
    if settings.is_live and g.get("user") is not None:
        return redirect(url_for("views.chat_page"))
    return render_template(
        "setup.html",
        app_version=__version__,
        mode=settings.mode,
        first_run=AuthStore().count() == 0,
    )


@views.get("/")
def chat_page():
    return _page(
        "chat.html",
        "Conversations",
        "Search, score, tailor and track — with approval on every external action.",
        "chat",
    )


@views.get("/jobs")
def jobs_page():
    return _page(
        "jobs.html", "Job board", "Live postings, hybrid scores and verified tailoring.", "jobs"
    )


@views.get("/applications")
def applications_page():
    return _page(
        "applications.html",
        "Applications",
        "A board for every role you are pursuing.",
        "applications",
    )


@views.get("/memory")
def memory_page():
    return _page(
        "memory.html", "Memory", "Everything JobPilot remembers, and nothing it doesn't.", "memory"
    )


@views.get("/profile")
def profile_page():
    return _page(
        "profile.html",
        "Profile",
        "The single source of truth for every generated claim.",
        "profile",
    )


@views.get("/system")
def system_page():
    return _page(
        "system.html", "System", "Sources, models, budget, guardrails and run traces.", "system"
    )


@views.app_errorhandler(404)
def not_found(_error):
    if request.path.startswith("/api/"):
        return jsonify({"error": "Not found.", "status": 404}), 404
    return _page("error.html", "Not found", "That page does not exist.", ""), 404


@views.app_errorhandler(500)
def server_error(_error):
    if request.path.startswith("/api/"):
        return jsonify({"error": "Server error.", "status": 500}), 500
    return _page("error.html", "Something broke", "Check the terminal for the traceback.", ""), 500
