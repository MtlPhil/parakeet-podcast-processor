"""Built-in accounts (P3_AUTH=local) and the setup wizard's endpoints."""

import os
import stat

import pytest
from fastapi.testclient import TestClient

import p3.api.deps as deps
from p3.api import auth
from p3.api.main import app
from p3.api.routers import setup as setup_routes

PASSWORD = "correct horse"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Local-auth mode on a fresh database; secrets and server.env in tmp."""
    deps.close_db()
    monkeypatch.setattr(deps, "_DB_PATH", str(tmp_path / "test.duckdb"))
    monkeypatch.setattr(deps, "_CONFIG_PATH", str(tmp_path / "feeds.yaml"))
    deps.save_config({"feeds": [], "settings": {}})
    monkeypatch.setenv("P3_AUTH", "local")
    monkeypatch.setenv("P3_DEV_USER", "intruder")  # must be ignored
    monkeypatch.delenv("P3_MANAGED", raising=False)
    monkeypatch.setattr(auth, "_FAILED_LOGIN_DELAY", 0, raising=False)
    from p3.api.routers import auth as auth_routes

    monkeypatch.setattr(auth_routes, "_FAILED_LOGIN_DELAY", 0)
    secrets = {
        name: tmp_path / f"{name}.txt" for name in setup_routes._SECRET_FILES
    }
    monkeypatch.setattr(setup_routes, "_SECRET_FILES", secrets)
    monkeypatch.setattr(setup_routes, "SERVER_ENV", tmp_path / "server.env")
    yield tmp_path
    deps.close_db()


def _client():
    return TestClient(app)


def _setup_admin(client, username="ann"):
    resp = client.post(
        "/api/auth/setup", json={"username": username, "password": PASSWORD}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestPasswords:
    def test_hash_round_trip_and_salting(self):
        h1, h2 = auth.hash_password("secret pass"), auth.hash_password("secret pass")
        assert h1 != h2
        assert auth.verify_password("secret pass", h1)
        assert not auth.verify_password("wrong pass", h1)
        assert not auth.verify_password("secret pass", None)
        assert not auth.verify_password("secret pass", "garbage")


class TestFirstRun:
    def test_status_before_and_after_setup(self, env):
        c = _client()
        assert c.get("/api/auth/status").json() == {
            "mode": "local",
            "setup_needed": True,
            "signed_in": False,
            "busy": False,
        }
        user = _setup_admin(c)
        assert user["role"] == "admin"
        status = c.get("/api/auth/status").json()
        assert status["setup_needed"] is False and status["signed_in"] is True
        assert c.get("/api/me").json()["username"] == "ann"

    def test_setup_only_once(self, env):
        _setup_admin(_client())
        resp = _client().post(
            "/api/auth/setup", json={"username": "eve", "password": PASSWORD}
        )
        assert resp.status_code == 409

    def test_rejects_short_password_and_bad_username(self, env):
        c = _client()
        assert c.post("/api/auth/setup", json={"username": "ann", "password": "short"}).status_code == 400
        assert c.post("/api/auth/setup", json={"username": "../x", "password": PASSWORD}).status_code == 400
        assert c.get("/api/auth/status").json()["setup_needed"] is True

    def test_proxy_header_and_dev_user_are_ignored(self, env):
        _setup_admin(_client())
        c = _client()
        assert c.get("/api/me").status_code == 401
        assert c.get("/api/me", headers={"Remote-User": "ann"}).status_code == 401


class TestSignIn:
    def test_login_logout(self, env):
        _setup_admin(_client())
        c = _client()
        assert c.post("/api/auth/login", json={"username": "ann", "password": "nope nope"}).status_code == 401
        assert c.post("/api/auth/login", json={"username": "ghost", "password": PASSWORD}).status_code == 401
        assert c.post("/api/auth/login", json={"username": "ann", "password": PASSWORD}).status_code == 200
        assert c.get("/api/me").status_code == 200
        c.post("/api/auth/logout")
        assert c.get("/api/me").status_code == 401

    def test_password_hash_never_leaves_the_server(self, env):
        c = _client()
        _setup_admin(c)
        assert "password_hash" not in c.get("/api/me").text
        assert "password_hash" not in c.get("/api/admin/users").text

    def test_change_password_signs_out_other_sessions(self, env):
        a = _client()
        _setup_admin(a)
        b = _client()
        b.post("/api/auth/login", json={"username": "ann", "password": PASSWORD})
        resp = a.post(
            "/api/me/password",
            json={"current_password": PASSWORD, "new_password": "new password 1"},
        )
        assert resp.status_code == 200
        assert a.get("/api/me").status_code == 200
        assert b.get("/api/me").status_code == 401
        c = _client()
        assert c.post("/api/auth/login", json={"username": "ann", "password": "new password 1"}).status_code == 200


class TestAdminAccounts:
    def test_admin_creates_users_with_roles(self, env):
        admin = _client()
        _setup_admin(admin)
        resp = admin.post(
            "/api/admin/users",
            json={"username": "bob", "password": PASSWORD, "role": "user"},
        )
        assert resp.status_code == 200 and resp.json()["role"] == "user"
        resp = admin.post(
            "/api/admin/users",
            json={"username": "cat", "password": PASSWORD, "role": "admin"},
        )
        assert resp.json()["role"] == "admin"
        assert admin.post(
            "/api/admin/users", json={"username": "bob", "password": PASSWORD}
        ).status_code == 409

        bob = _client()
        bob.post("/api/auth/login", json={"username": "bob", "password": PASSWORD})
        assert bob.get("/api/me").json()["role"] == "user"
        assert bob.post(
            "/api/admin/users", json={"username": "dan", "password": PASSWORD}
        ).status_code == 403

    def test_admin_resets_a_password(self, env):
        admin = _client()
        _setup_admin(admin)
        admin.post("/api/admin/users", json={"username": "bob", "password": PASSWORD})
        bob = _client()
        bob.post("/api/auth/login", json={"username": "bob", "password": PASSWORD})
        assert admin.put(
            "/api/admin/users/bob/password", json={"password": "reset pass 2"}
        ).status_code == 200
        assert bob.get("/api/me").status_code == 401
        assert _client().post("/api/auth/login", json={"username": "bob", "password": "reset pass 2"}).status_code == 200


class TestProxyModeUnchanged:
    def test_header_still_signs_in_and_local_routes_refuse(self, env, monkeypatch):
        monkeypatch.setenv("P3_AUTH", "proxy")
        c = _client()
        assert c.get("/api/me", headers={"Remote-User": "pat"}).json()["username"] == "pat"
        assert c.get("/api/auth/status").json()["mode"] == "proxy"
        assert c.post("/api/auth/setup", json={"username": "x1", "password": PASSWORD}).status_code == 400


class TestWizardEndpoints:
    def test_progress(self, env):
        admin = _client()
        _setup_admin(admin)
        assert admin.get("/api/setup/progress").json() == {"done": False, "shared_done": False}
        assert admin.post("/api/setup/progress").json() == {"done": True, "shared_done": True}
        admin.post("/api/admin/users", json={"username": "bob", "password": PASSWORD})
        bob = _client()
        bob.post("/api/auth/login", json={"username": "bob", "password": PASSWORD})
        assert bob.get("/api/setup/progress").json() == {"done": False, "shared_done": True}

    def test_secrets_are_written_privately_and_never_returned(self, env):
        admin = _client()
        _setup_admin(admin)
        resp = admin.put("/api/setup/secrets", json={"gemini_api_key": " abc123 "})
        assert resp.json() == {"gemini_api_key": True, "openai_api_key": False, "smtp_password": False}
        path = env / "gemini_api_key.txt"
        assert path.read_text().strip() == "abc123"
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert "abc123" not in admin.get("/api/setup/secrets").text
        admin.put("/api/setup/secrets", json={"gemini_api_key": ""})
        assert not path.exists()

    def test_network_writes_server_env_and_refuses_while_busy(self, env):
        admin = _client()
        _setup_admin(admin)
        data = admin.get("/api/setup/network").json()
        assert data["lan"] is False and data["managed"] is False
        db = deps.get_db()
        db.create_job("fetch")
        assert admin.put("/api/setup/network", json={"lan": True}).status_code == 409
        resp = admin.put("/api/setup/network", json={"lan": True, "force": True})
        assert resp.json()["lan"] is True
        assert "P3_HOST=0.0.0.0" in (env / "server.env").read_text()

    def test_wizard_admin_endpoints_need_an_admin(self, env):
        admin = _client()
        _setup_admin(admin)
        admin.post("/api/admin/users", json={"username": "bob", "password": PASSWORD})
        bob = _client()
        bob.post("/api/auth/login", json={"username": "bob", "password": PASSWORD})
        for method, path in (("get", "/api/setup/secrets"), ("get", "/api/setup/network"), ("get", "/api/setup/ollama")):
            assert getattr(bob, method)(path).status_code == 403


class TestEmailProblems:
    def test_messages_people_can_act_on(self):
        import smtplib

        s = {"smtp_host": "smtp.example.com", "smtp_port": 587}
        problem = setup_routes.email_problem
        assert "password" in problem(RuntimeError("No SMTP password found: set ..."), s)
        assert "mail server first" in problem(RuntimeError("smtp_host is not set in ..."), s)
        assert "didn't accept" in problem(smtplib.SMTPAuthenticationError(535, b"bad"), s)
        assert "smtp.example.com on port 587" in problem(ConnectionRefusedError(), s)
        assert problem(ValueError("odd"), s) == "Could not send: odd"
