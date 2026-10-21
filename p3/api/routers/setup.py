"""The setup wizard's server side: progress, secrets, Ollama check, test
email, and whether the server listens on localhost only or the LAN.

Secrets are written to the gitignored files the app already reads
(``config/*.txt``, mode 600) and are never sent back, only whether each is
set. The network choice lives in ``config/server.env``, read by
``bin/p3-serve`` (the launcher the Mac installer sets up); changing it
restarts the server through launchd when it runs under that launcher
(``P3_MANAGED=1``).
"""

import os
import signal
import smtplib
import socket
import subprocess
import threading
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from p3.api.auth import current_user, require_admin
from p3.api.deps import get_db, load_config, user_settings
from p3.llm import DEFAULT_OLLAMA_URL, GEMINI_API_KEY_FILE, OPENAI_API_KEY_FILE
from p3.mailer import SMTP_PASSWORD_FILE, send_email

router = APIRouter(prefix="/api/setup", tags=["setup"])

SERVER_ENV = Path("config/server.env")
DEFAULT_PORT = 8000
LOCAL_HOST, LAN_HOST = "127.0.0.1", "0.0.0.0"

_SECRET_FILES = {
    "gemini_api_key": GEMINI_API_KEY_FILE,
    "openai_api_key": OPENAI_API_KEY_FILE,
    "smtp_password": SMTP_PASSWORD_FILE,
}


def _done_key(username: str) -> str:
    return f"setup_done:{username}"


SHARED_DONE_KEY = "setup_done"


# ---------------------------------------------------------------- progress


class Progress(BaseModel):
    done: bool
    shared_done: bool


@router.get("/progress", response_model=Progress)
def get_progress(user: dict = Depends(current_user)):
    """Whether this user finished (or skipped) the wizard, and whether an
    admin has done the shared part."""
    db = get_db()
    return {
        "done": db.get_state(_done_key(user["username"])) == "1",
        "shared_done": db.get_state(SHARED_DONE_KEY) == "1",
    }


@router.post("/progress", response_model=Progress)
def finish(user: dict = Depends(current_user)):
    """Mark the wizard finished for this user (and the shared part, for an
    admin). It stays available from Settings."""
    db = get_db()
    db.set_state(_done_key(user["username"]), "1")
    if user["role"] == "admin":
        db.set_state(SHARED_DONE_KEY, "1")
    return get_progress(user)


# ----------------------------------------------------------------- secrets


class SecretsOut(BaseModel):
    gemini_api_key: bool
    openai_api_key: bool
    smtp_password: bool


class SecretsIn(BaseModel):
    """null leaves a secret unchanged; an empty string removes it."""

    gemini_api_key: Optional[str] = None
    openai_api_key: Optional[str] = None
    smtp_password: Optional[str] = None


def _secrets_status() -> dict:
    return {
        name: path.exists() and bool(path.read_text().strip())
        for name, path in _SECRET_FILES.items()
    }


@router.get("/secrets", response_model=SecretsOut)
def get_secrets(_admin: dict = Depends(require_admin)):
    return _secrets_status()


@router.put("/secrets", response_model=SecretsOut)
def set_secrets(body: SecretsIn, _admin: dict = Depends(require_admin)):
    for name, value in body.model_dump().items():
        if value is None:
            continue
        path = _SECRET_FILES[name]
        value = value.strip()
        if not value:
            path.unlink(missing_ok=True)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(value + "\n")
        os.chmod(path, 0o600)
    return _secrets_status()


# ------------------------------------------------------------------ ollama


class OllamaOut(BaseModel):
    reachable: bool
    url: str
    models: list[str] = []


@router.get("/ollama", response_model=OllamaOut)
def check_ollama(_admin: dict = Depends(require_admin)):
    """Is Ollama running, and which models does it have?"""
    url = (
        load_config().get("settings", {}).get("ollama_base_url") or DEFAULT_OLLAMA_URL
    ).rstrip("/")
    try:
        resp = httpx.get(f"{url}/api/tags", timeout=3.0)
        resp.raise_for_status()
        models = [m.get("name", "") for m in resp.json().get("models", [])]
        return {"reachable": True, "url": url, "models": [m for m in models if m]}
    except Exception:
        return {"reachable": False, "url": url, "models": []}


# -------------------------------------------------------------- test email


class TestEmailIn(BaseModel):
    to: Optional[str] = None


@router.post("/test-email", response_model=dict)
def test_email(body: TestEmailIn, user: dict = Depends(current_user)):
    """Send a test message with the saved email settings."""
    settings = user_settings(user["username"])
    to = (body.to or settings.get("email") or user.get("email") or "").strip()
    if not to:
        raise HTTPException(400, "No email address to send to")
    try:
        send_email(
            settings,
            to,
            "P3 test email",
            "Email from P3 works. Daily digests will arrive like this.",
            "<p>Email from P3 works. Daily digests will arrive like this.</p>",
        )
    except Exception as e:
        raise HTTPException(400, email_problem(e, settings))
    return {"sent": True, "to": to}


def email_problem(error: Exception, settings: dict) -> str:
    """A sending failure in words a non-technical person can act on."""
    text = str(error)
    if "smtp_host is not set" in text:
        return "Fill in the mail server first."
    if "No SMTP password" in text:
        return (
            "Enter the email account's password first. With Gmail, that's an "
            "app password, not your normal password."
        )
    if isinstance(error, smtplib.SMTPAuthenticationError):
        return (
            "The mail server didn't accept the account and password. With Gmail, "
            "use an app password, not your normal password."
        )
    if isinstance(
        error, (OSError, smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected)
    ):
        host = settings.get("smtp_host")
        port = settings.get("smtp_port") or 587
        return f"Couldn't reach the mail server {host} on port {port}. Check both, and that this Mac is online."
    return f"Could not send: {text}"


# ----------------------------------------------------------------- network


class NetworkOut(BaseModel):
    lan: bool
    port: int
    managed: bool
    addresses: list[str] = []


class NetworkIn(BaseModel):
    lan: bool
    force: bool = False


def read_server_env() -> dict:
    values = {}
    if SERVER_ENV.exists():
        for line in SERVER_ENV.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"')
    return values


def write_server_env(values: dict):
    SERVER_ENV.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Read by bin/p3-serve. Changed from P3's Settings or setup wizard."]
    lines += [f"{key}={value}" for key, value in values.items()]
    SERVER_ENV.write_text("\n".join(lines) + "\n")


def _port(values: dict) -> int:
    try:
        return int(values.get("P3_PORT") or os.environ.get("P3_PORT") or DEFAULT_PORT)
    except ValueError:
        return DEFAULT_PORT


def lan_addresses(port: int) -> list[str]:
    """Addresses other devices on the network can use."""
    urls = []
    try:
        name = subprocess.run(
            ["scutil", "--get", "LocalHostName"],
            capture_output=True,
            text=True,
            timeout=3,
        ).stdout.strip()
        if name:
            urls.append(f"http://{name}.local:{port}")
    except Exception:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 80))  # no packet is sent for UDP connect
            urls.append(f"http://{s.getsockname()[0]}:{port}")
    except OSError:
        pass
    return urls


def _network_status() -> dict:
    values = read_server_env()
    port = _port(values)
    lan = values.get("P3_HOST", LOCAL_HOST) == LAN_HOST
    return {
        "lan": lan,
        "port": port,
        "managed": os.environ.get("P3_MANAGED") == "1",
        "addresses": lan_addresses(port),
    }


def _restart_soon(delay: float = 1.0):
    """Stop this server shortly (after the response goes out); launchd's
    KeepAlive starts it again with the new settings."""
    threading.Timer(delay, os.kill, args=(os.getpid(), signal.SIGTERM)).start()


@router.get("/network", response_model=NetworkOut)
def get_network(_admin: dict = Depends(require_admin)):
    return _network_status()


@router.put("/network", response_model=NetworkOut)
def set_network(body: NetworkIn, _admin: dict = Depends(require_admin)):
    """Listen on localhost only, or on the LAN too. Restarts the server, so
    it refuses while jobs run unless ``force``."""
    values = read_server_env()
    values.setdefault("P3_PORT", str(_port(values)))
    wanted = LAN_HOST if body.lan else LOCAL_HOST
    if values.get("P3_HOST", LOCAL_HOST) == wanted:
        return _network_status()
    active = get_db().get_active_jobs()
    if active and not body.force:
        raise HTTPException(
            409,
            f"{len(active)} job(s) are running; changing this restarts P3 and "
            "would interrupt them. Try again when they finish.",
        )
    values["P3_HOST"] = wanted
    write_server_env(values)
    status = _network_status()
    if status["managed"]:
        _restart_soon()
    return status
