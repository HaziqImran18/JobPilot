"""Flask application factory with account sessions and per-user workspaces."""

from __future__ import annotations

import os
import threading
from datetime import UTC, datetime, timedelta

from flask import Flask, Response, g, jsonify, redirect, request, session, url_for
from werkzeug.exceptions import HTTPException

from jobpilot import __version__
from jobpilot.bootstrap import bootstrap, bootstrap_workspace
from jobpilot.core.auth import AuthStore
from jobpilot.core.config import Settings, get_settings
from jobpilot.core.user_context import clear_current_user, set_current_user
from jobpilot.ui.api import api
from jobpilot.ui.views import csrf_token, views

_PUBLIC_PATHS = {"/setup", "/login", "/register", "/logout", "/api/health"}


def create_app(settings: Settings | None = None, *, preload_mcp: bool | None = None) -> Flask:
    settings = settings or get_settings()
    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
        static_url_path="/static",
    )
    app.config.update(
        SECRET_KEY=settings.secret_key,
        MAX_CONTENT_LENGTH=12 * 1024 * 1024,
        JSON_SORT_KEYS=False,
        PREFERRED_URL_SCHEME="http",
        TEMPLATES_AUTO_RELOAD=settings.debug,
        SESSION_COOKIE_NAME="jobpilot_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=settings.env == "production",
        PERMANENT_SESSION_LIFETIME=timedelta(days=settings.session_days),
    )
    bootstrap()
    app.register_blueprint(api)
    app.register_blueprint(views)

    @app.context_processor
    def inject_globals():
        return {
            "app_version": __version__,
            "mode": settings.mode,
            "year": datetime.now(UTC).year,
            "csrf_token": csrf_token,
            "current_user": g.get("user"),
        }

    @app.before_request
    def load_account():
        user_id = session.get("user_id", "")
        user = AuthStore().get(user_id) if user_id else None
        if user is None:
            session.pop("user_id", None)
            clear_current_user()
            g.user = None
            return None
        g.user = user
        set_current_user(user.id)
        bootstrap_workspace(user.id)
        return None

    @app.before_request
    def require_model():
        if settings.is_live:
            return None
        if request.path.startswith("/static/") or request.path in _PUBLIC_PATHS:
            return None
        if request.path.startswith("/api/"):
            return (
                jsonify(
                    {
                        "error": (
                            "The AI model key is not configured. Add OPENAI_API_KEY "
                            "to .env and restart."
                        ),
                        "code": "model_not_configured",
                    }
                ),
                503,
            )
        return redirect(url_for("views.setup"))

    @app.before_request
    def require_account():
        if request.path.startswith("/static/") or request.path in _PUBLIC_PATHS:
            return None
        if g.get("user") is not None:
            return None
        if request.path.startswith("/api/"):
            return (
                jsonify(
                    {
                        "error": "Sign in to use this workspace.",
                        "code": "auth_required",
                    }
                ),
                401,
            )
        if AuthStore().count() == 0:
            return redirect(url_for("views.register_page"))
        return redirect(url_for("views.login_page"))

    @app.before_request
    def reject_cross_site_writes():
        # API writes use a custom header. HTML forms carry a session-bound token
        # that is validated by the login, registration and logout routes.
        if (
            request.path.startswith("/api/")
            and request.method in {"POST", "PUT", "PATCH", "DELETE"}
            and request.headers.get("X-JobPilot") != "1"
        ):
            return jsonify({"error": "Missing X-JobPilot header."}), 403
        return None

    @app.teardown_request
    def clear_request_user(_error=None):
        clear_current_user()

    @app.after_request
    def security_headers(response: Response) -> Response:
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'; "
            "form-action 'self'; frame-ancestors 'self'",
        )
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        elif request.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.errorhandler(HTTPException)
    def handle_http_error(error: HTTPException):
        if request.path.startswith("/api/"):
            return jsonify({"error": error.description, "status": error.code}), error.code or 500
        return error

    should_preload = settings.preload_mcp if preload_mcp is None else preload_mcp
    if should_preload and (not settings.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true"):
        threading.Thread(target=_preload_mcp, name="jobpilot-preload", daemon=True).start()

    return app


def _preload_mcp() -> None:
    """Warm each existing account's MCP servers (opt-in; off by default)."""
    try:
        from jobpilot.harness.mcp_client import get_mcp_manager

        for user in AuthStore().list_users():
            get_mcp_manager(user.id).start()
    except Exception:
        # The System page surfaces startup errors; the app still works without tools.
        return


app = create_app()


def main() -> None:
    settings = get_settings()
    app.run(
        host=settings.host,
        port=settings.port,
        debug=settings.debug,
        use_reloader=False,
        threaded=True,
    )


if __name__ == "__main__":
    main()
