"""Tests for serving the built frontend with client-side route fallback."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from p3.api.main import SPAStaticFiles


@pytest.fixture
def client(tmp_path):
    (tmp_path / "index.html").write_text("<html>app</html>")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.js").write_text("console.log('app')")

    app = FastAPI()

    @app.get("/api/ping")
    def ping():
        return {"ok": True}

    app.mount("/", SPAStaticFiles(directory=str(tmp_path), html=True), name="frontend")
    return TestClient(app)


def test_root_serves_index(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "app" in resp.text


def test_deep_link_falls_back_to_index(client):
    resp = client.get("/podcasts/3")
    assert resp.status_code == 200
    assert resp.text == "<html>app</html>"


def test_existing_asset_is_served(client):
    resp = client.get("/assets/app.js")
    assert resp.status_code == 200
    assert "console.log" in resp.text


def test_missing_asset_is_404(client):
    assert client.get("/assets/missing.js").status_code == 404


def test_unknown_api_path_is_404(client):
    assert client.get("/api/does-not-exist").status_code == 404


def test_api_routes_take_precedence(client):
    assert client.get("/api/ping").json() == {"ok": True}
