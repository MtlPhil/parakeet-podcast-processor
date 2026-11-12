"""API keys and passwords.

Each secret is read from its environment variable or, failing that, from a
file of its own in the config folder (readable by its owner only). The
setup wizard writes those files.
"""

import os
from pathlib import Path
from typing import Optional

from . import paths

# Secret name (also its file's name, plus ".txt") to environment variable.
ENV_VARS = {
    "gemini_api_key": "GEMINI_API_KEY",
    "openai_api_key": "OPENAI_API_KEY",
    "smtp_password": "SMTP_PASSWORD",
}


def secret_file(name: str) -> Path:
    return paths.config_dir() / f"{name}.txt"


def read(name: str) -> Optional[str]:
    """The secret, or None if it is set nowhere."""
    return os.environ.get(ENV_VARS[name]) or _from_file(name)


def is_saved(name: str) -> bool:
    """Whether the secret's file holds one (the environment doesn't count:
    this is what the setup wizard reports and can change)."""
    return _from_file(name) is not None


def write(name: str, value: str):
    """Save the secret to its file; an empty value removes the file."""
    path = secret_file(name)
    value = value.strip()
    if not value:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(value + "\n")
    os.chmod(path, 0o600)


def missing(name: str) -> str:
    """What to tell someone whose secret is not set."""
    return f"set {ENV_VARS[name]} or create {secret_file(name)}"


def _from_file(name: str) -> Optional[str]:
    path = secret_file(name)
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8").strip() or None
