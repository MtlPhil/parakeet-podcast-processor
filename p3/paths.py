"""Where P3 keeps its files.

Everything P3 reads and writes lives under one home folder: the folder named
by the P3_HOME environment variable or, without it, the current directory
(how P3 has always run: from its own folder). Looked up on each call, so a
changed variable or directory takes effect.
"""

import os
from pathlib import Path
from typing import Optional

HOME_ENV = "P3_HOME"

# Folders of generated files, each holding one subfolder per user.
CONTENT_BASES = ("exports", "blog_posts", "linkedin_posts")


def home() -> Path:
    return Path(os.environ.get(HOME_ENV) or ".")


def data_dir() -> Path:
    """Downloaded audio and the database."""
    return home() / "data"


def database() -> Path:
    return data_dir() / "p3.duckdb"


def config_dir() -> Path:
    """The settings file, the secrets and the server's own settings."""
    return home() / "config"


def config_file() -> Path:
    return config_dir() / "feeds.yaml"


def server_env() -> Path:
    """Host and port, read by bin/p3-serve."""
    return config_dir() / "server.env"


def content_dir(base: str, username: Optional[str] = None) -> Path:
    """A folder of generated files (one of CONTENT_BASES): a user's own, or
    without a username the folder itself (CLI output)."""
    folder = home() / base
    return folder / username if username else folder


def frontend_build() -> Path:
    """The built web app: next to the code, where the installer and a source
    checkout have it."""
    return Path(__file__).resolve().parent.parent / "frontend" / "dist"


def ensure():
    """Create the folders P3 expects to find."""
    for folder in (
        data_dir() / "audio",
        config_dir(),
        *map(content_dir, CONTENT_BASES),
    ):
        folder.mkdir(parents=True, exist_ok=True)
