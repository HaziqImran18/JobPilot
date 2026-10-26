"""Live-only product guard: no key means setup instructions, not a fake AI."""

from __future__ import annotations

from jobpilot.core.config import get_settings
from jobpilot.ui.app import create_app


def _client_without_key():
    settings = get_settings().model_copy(
        update={"openai_api_key": "", "preload_mcp": False}
    )
    app = create_app(settings, preload_mcp=False)
    app.config.update(TESTING=True)
    return app.test_client()


def test_pages_redirect_to_setup(clean_database):
    client = _client_without_key()
    response = client.get("/")
    assert response.status_code == 302
    assert "/setup" in response.headers["Location"]

    setup = client.get("/setup")
    assert setup.status_code == 200
    assert b"Connect the AI model" in setup.data
    assert b"OPENAI_API_KEY" in setup.data


def test_api_fails_with_clear_error(clean_database):
    client = _client_without_key()
    response = client.get("/api/system/stats")
    assert response.status_code == 503
    assert response.json["code"] == "model_not_configured"
    assert "OPENAI_API_KEY" in response.json["error"]


def test_static_and_setup_stay_available(clean_database):
    client = _client_without_key()
    assert client.get("/static/css/app.css").status_code == 200
    assert client.get("/setup").status_code == 200
