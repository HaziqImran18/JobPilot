"""Route-level tests for the Flask UI and JSON API."""

from __future__ import annotations

from pathlib import Path

from jobpilot import __version__
from jobpilot.core.auth import AuthStore
from jobpilot.core.config import get_settings
from jobpilot.core.db import get_db
from jobpilot.core.models import Profile
from jobpilot.core.user_context import user_context
from jobpilot.evals.fixtures import seed_eval_workspace
from jobpilot.services.profile import ProfileService
from jobpilot.ui.app import create_app

HEADERS = {"X-JobPilot": "1"}
TEST_PASSWORD = "jobpilot-test-password"


def _login(as_client, user_id: str) -> None:
    with as_client.session_transaction() as session:
        session["user_id"] = user_id
        session["_csrf"] = "test-csrf-token"


def test_pages_render_with_the_shared_shell(client):
    for path, marker in [
        ("/", "New conversation"),
        ("/jobs", "Postings"),
        ("/applications", "Pipeline"),
        ("/memory", "Persistent memory"),
        ("/profile", "Search preferences"),
        ("/system", "Health checks"),
    ]:
        response = client.get(path)
        assert response.status_code == 200
        assert b"JobPilot" in response.data
        assert marker.encode() in response.data
        csp = response.headers["Content-Security-Policy"]
        assert "script-src 'self'" in csp
        assert "https://" not in csp


def test_api_health_bootstrap_and_json_errors(client):
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json["ok"] is True
    assert health.json["version"] == __version__

    bootstrap = client.get("/api/bootstrap")
    assert bootstrap.status_code == 200
    assert bootstrap.json["profile"]["name"]
    assert "daily_spent" in bootstrap.json["budget"]

    missing = client.get("/api/does-not-exist")
    assert missing.status_code == 404
    assert missing.is_json
    assert missing.json["status"] == 404

    page = client.get("/does-not-exist")
    assert page.status_code == 404
    assert b"JobPilot" in page.data


def test_writes_require_the_jobpilot_header(client):
    assert client.post("/api/sessions").status_code == 403
    created = client.post("/api/sessions", headers=HEADERS)
    assert created.status_code == 201
    session_id = created.json["session"]["id"]
    assert client.delete(f"/api/sessions/{session_id}", headers=HEADERS).status_code == 200


def test_jobs_list_filter_and_detail(client, seeded_workspace):
    listing = client.get("/api/jobs?limit=5&sort=score")
    assert listing.status_code == 200
    assert listing.json["total"] == 10
    assert len(listing.json["items"]) == 5

    ranked = client.post(
        "/api/jobs/rank", headers=HEADERS, json={"query": "AI Engineer", "limit": 5}
    )
    assert ranked.status_code == 200
    assert len(ranked.json["ranked"]) == 5
    first = ranked.json["ranked"][0]["job"]
    assert first["id"].startswith("job_")

    detail = client.get(f"/api/jobs/{first['id']}")
    assert detail.status_code == 200
    assert detail.json["job"]["title"] == first["title"]
    assert detail.json["job"]["score"] >= 50

    bad_score = client.get("/api/jobs?min_score=not-a-number")
    assert bad_score.status_code == 400
    assert bad_score.is_json


def test_tailor_track_update_and_delete_application(client, seeded_workspace):
    jobs = client.get("/api/jobs?sort=score&limit=1").json["items"]
    job = jobs[0]

    tailored = client.post(
        f"/api/jobs/{job['id']}/tailor", headers=HEADERS, json={"use_llm": False}
    )
    assert tailored.status_code == 200
    result = tailored.json["tailoring"]
    assert result["verified"] is True
    assert result["bullets"]
    assert result["cv_path"].endswith(".md")

    filename = Path(result["cv_path"]).name
    download = client.get(f"/api/files/{filename}")
    assert download.status_code == 200
    assert b"AI Engineer" in download.data
    assert client.get("/api/files/..%2F..%2Fjobpilot.db").status_code in {400, 404}
    assert client.get("/api/files/not-markdown.txt").status_code == 400

    tracked = client.post(
        f"/api/jobs/{job['id']}/track", headers=HEADERS, json={"cv_path": result["cv_path"]}
    )
    assert tracked.status_code == 201
    application = tracked.json["application"]
    assert application["status"] == "saved"
    assert application["cv_path"] == result["cv_path"]
    application_id = application["id"]

    board = client.get("/api/applications")
    assert board.status_code == 200
    assert board.json["stats"]["total"] == 1

    detail = client.get(f"/api/applications/{application_id}")
    assert detail.status_code == 200
    assert detail.json["application"]["job_title"] == job["title"]

    moved = client.patch(
        f"/api/applications/{application_id}",
        headers=HEADERS,
        json={
            "status": "applying",
            "notes": "Waiting for a reply.",
            "next_action_at": "2026-11-01",
        },
    )
    assert moved.status_code == 400  # not a valid pipeline stage; strict validation

    moved = client.patch(
        f"/api/applications/{application_id}",
        headers=HEADERS,
        json={"status": "applied", "notes": "Waiting for a reply.", "next_action_at": "2026-11-01"},
    )
    assert moved.status_code == 200
    assert moved.json["application"]["status"] == "applied"
    assert moved.json["application"]["next_action_at"] == "2026-11-01"

    detail = client.get(f"/api/applications/{application_id}")
    assert any(event["status"] == "applied" for event in detail.json["application"]["events"])

    deleted = client.delete(f"/api/applications/{application_id}", headers=HEADERS)
    assert deleted.status_code == 200
    assert client.get(f"/api/applications/{application_id}").status_code == 404


def test_memory_crud_and_recall(client):
    created = client.post(
        "/api/memories",
        headers=HEADERS,
        json={
            "text": "I prefer remote-first product teams.",
            "kind": "semantic",
            "importance": 0.9,
        },
    )
    assert created.status_code == 201
    memory = created.json["memory"]
    assert memory["status"] == "active"

    listed = client.get("/api/memories?status=all")
    assert listed.status_code == 200
    assert len(listed.json["memories"]) == 1

    recalled = client.get("/api/memories/recall?query=remote-first%20product%20teams")
    assert recalled.status_code == 200
    assert recalled.json["memories"][0]["id"] == memory["id"]

    assert client.delete(f"/api/memories/{memory['id']}", headers=HEADERS).status_code == 200
    assert client.get("/api/memories").json["memories"] == []
    forgotten = client.get("/api/memories?status=forgotten").json["memories"]
    assert forgotten and forgotten[0]["id"] == memory["id"]


def test_profile_round_trip(client):
    current = client.get("/api/profile")
    assert current.status_code == 200
    assert current.json["profile"]["full_name"] == ""
    payload = current.json["profile"]
    payload["headline"] = "AI engineer focused on reliable agents."
    saved = client.put("/api/profile", headers=HEADERS, json=payload)
    assert saved.status_code == 200
    assert saved.json["profile"]["headline"] == payload["headline"]
    assert saved.json["version"] != current.json["version"]

    fetched = client.get("/api/profile")
    assert fetched.json["profile"]["skills"] == payload["skills"]
    assert fetched.json["profile"]["experiences"] == payload["experiences"]


def test_system_stats_doctor_traces_and_usage(client):
    stats = client.get("/api/system/stats")
    assert stats.status_code == 200
    assert stats.json["mode"] in {"live", "unconfigured"}
    assert "counts" in stats.json

    report = client.get("/api/system/doctor")
    assert report.status_code == 200
    assert report.json["checks"]
    assert any(check["group"] == "llm" for check in report.json["checks"])

    assert client.get("/api/system/traces").status_code == 200
    assert client.get("/api/system/usage").status_code == 200
    assert client.get("/api/system/traces/run_missing").status_code == 404


def _app_client():
    settings = get_settings().model_copy(
        update={"openai_api_key": "test-key", "preload_mcp": False}
    )
    app = create_app(settings, preload_mcp=False)
    app.config.update(TESTING=True)
    return app.test_client()


def test_api_requires_an_account(clean_database, account):
    anonymous = _app_client()
    response = anonymous.get("/api/jobs")
    assert response.status_code == 401
    assert response.json["code"] == "auth_required"

    page = anonymous.get("/")
    assert page.status_code == 302
    assert "/login" in page.headers["Location"]


def test_register_login_and_logout_flow(clean_database, account):
    anonymous = _app_client()
    page = anonymous.get("/register")
    assert page.status_code == 200

    with anonymous.session_transaction() as session:
        session["_csrf"] = "register-csrf"
    created = anonymous.post(
        "/register",
        data={
            "_csrf": "register-csrf",
            "display_name": "Second Person",
            "email": "second@example.com",
            "password": "another-good-password",
            "password_confirm": "another-good-password",
        },
    )
    assert created.status_code == 302
    assert created.headers["Location"].endswith("/")
    assert AuthStore().count() == 2
    assert anonymous.get("/api/bootstrap").json["account"]["email"] == "second@example.com"

    with anonymous.session_transaction() as session:
        csrf = session["_csrf"]
    signed_out = anonymous.post("/logout", data={"_csrf": csrf})
    assert signed_out.status_code == 302
    assert anonymous.get("/api/jobs").status_code == 401

    with anonymous.session_transaction() as session:
        session["_csrf"] = "login-csrf"
    signed_in = anonymous.post(
        "/login",
        data={
            "_csrf": "login-csrf",
            "email": "second@example.com",
            "password": "another-good-password",
        },
    )
    assert signed_in.status_code == 302
    assert anonymous.get("/api/bootstrap").json["account"]["email"] == "second@example.com"

    bad = anonymous.get("/login")
    assert bad.status_code == 302  # already signed in


def test_accounts_have_isolated_workspaces(clean_database, account, client):
    other = AuthStore().register(
        email="other@example.com",
        password=TEST_PASSWORD,
        display_name="Other User",
    )
    with user_context(other.id):
        seed_eval_workspace(get_db())
        ProfileService().save(Profile(full_name="Other User"))

    current_jobs = client.get("/api/jobs").json
    assert current_jobs["total"] == 0
    assert client.get("/api/profile").json["profile"]["full_name"] == ""

    other_client = _app_client()
    _login(other_client, other.id)
    assert other_client.get("/api/jobs").json["total"] == 10
    assert other_client.get("/api/profile").json["profile"]["full_name"] == "Other User"
