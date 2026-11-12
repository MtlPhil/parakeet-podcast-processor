"""Shared dependencies for the API."""

import logging
import shutil
import unicodedata
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import yaml

from p3 import paths
from p3.database import P3Database
from p3.settings import SHARED_DEFAULTS

logger = logging.getLogger(__name__)

# Set by tests to use a database or settings file somewhere else.
_DB_PATH: Optional[str] = None
_CONFIG_PATH: Optional[str] = None

# Process-wide database instance; P3Database hands each thread its own cursor.
_db: P3Database | None = None


def get_db() -> P3Database:
    """Return the shared database instance, creating it if needed."""
    global _db
    if _db is None or _db.conn is None:
        _db = P3Database(_DB_PATH or str(paths.database()))
    return _db


def close_db():
    """Close the database connection."""
    global _db
    if _db is not None:
        _db.close()
        _db = None


def read_config(path: Path) -> dict:
    """The configuration file at ``path`` as ``{"feeds": [...], "settings":
    {...}}``. A missing file, an empty one, or a section left empty (YAML
    reads ``settings:`` with nothing under it as null) gives empty values."""
    config: dict[str, Any] = {}
    if path.exists():
        with open(path, encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
    config["feeds"] = config.get("feeds") or []
    config["settings"] = config.get("settings") or {}
    return config


def load_config() -> dict:
    """Load configuration from feeds.yaml."""
    return read_config(Path(_CONFIG_PATH) if _CONFIG_PATH else paths.config_file())


def save_config(config: dict):
    """Write configuration to feeds.yaml."""
    config_file = Path(_CONFIG_PATH) if _CONFIG_PATH else paths.config_file()
    config_file.parent.mkdir(parents=True, exist_ok=True)
    with open(config_file, "w", encoding="utf-8") as f:
        yaml.dump(
            config, f, default_flow_style=False, sort_keys=False, allow_unicode=True
        )


# Settings each user sets for themselves. A user without their own LLM or
# episode limit gets the shared setting of the same name, then these
# defaults. The rest are personal only: a value in the shared config is
# ignored, so e.g. no daily run starts for a user who never asked for one.
USER_SETTING_DEFAULTS: dict[str, Any] = {
    # "en", "fr" (Quebec French) or "original" (each episode's own language,
    # see exporter.shown_language): what pages show first
    "language": "en",
    "tone": "",  # instructions describing the user's voice, sent to the writer
    "llm_provider": "ollama",  # LLM for the user's writing: posts, synopses
    "llm_model": None,
    "gemini_model": None,
    "max_episodes_per_feed": SHARED_DEFAULTS["max_episodes_per_feed"],
    "daily_run_time": "04:00",  # "HH:MM"; set to "" to turn the daily run off
    "daily_run_linkedin": False,
    "daily_run_export": False,
    "daily_run_email": False,  # email the run's report (needs smtp_* settings)
    "email": "",  # where that report goes
}
_SHARED_FALLBACK_KEYS = (
    "llm_provider",
    "llm_model",
    "gemini_model",
    "max_episodes_per_feed",
)


def user_settings(username: Optional[str]) -> dict:
    """The shared settings overlaid with ``username``'s own settings.

    Without a username (CLI, config-file imports) only the shared settings
    and the defaults apply.
    """
    shared = load_config().get("settings") or {}
    settings = {k: v for k, v in USER_SETTING_DEFAULTS.items() if v is not None}
    settings.update(
        {
            k: v
            for k, v in shared.items()
            if k not in USER_SETTING_DEFAULTS or k in _SHARED_FALLBACK_KEYS
        }
    )
    user = get_db().get_user(username) if username else None
    if user:
        settings.update(
            {
                k: v
                for k, v in user["settings"].items()
                if k in USER_SETTING_DEFAULTS and v is not None
            }
        )
    return settings


def attachment(filename: str) -> dict[str, str]:
    """Response headers that make a download save as ``filename``.

    A header holds Latin-1 only (a title with "œ", or in Japanese, made the
    response fail), so the name goes twice, as RFC 6266 has it: plain ASCII
    for old clients, then the real name percent-encoded.
    """
    plain = unicodedata.normalize("NFKD", filename).encode("ascii", "ignore").decode()
    plain = plain.replace('"', "").strip() or "download"
    return {
        "Content-Disposition": f'attachment; filename="{plain}"; '
        f"filename*=UTF-8''{quote(filename, safe='')}"
    }


def delete_content(username: str):
    """Delete a user's generated files (posts, exports)."""
    for base in paths.CONTENT_BASES:
        folder = paths.content_dir(base, username)
        # A safeguard for a recursive delete: only ever a direct subfolder.
        if folder.parent != paths.content_dir(base) or not folder.is_dir():
            continue
        try:
            shutil.rmtree(folder)
        except OSError:
            logger.exception("Could not delete %s", folder)
