"""Shared dependencies for the API."""

from pathlib import Path
from typing import Any, Optional

import yaml

from p3.database import P3Database

_DB_PATH = "data/p3.duckdb"
_CONFIG_PATH = "config/feeds.yaml"

# Process-wide database instance; P3Database hands each thread its own cursor.
_db: P3Database | None = None


def get_db() -> P3Database:
    """Return the shared database instance, creating it if needed."""
    global _db
    if _db is None or _db.conn is None:
        _db = P3Database(_DB_PATH)
    return _db


def close_db():
    """Close the database connection."""
    global _db
    if _db is not None:
        _db.close()
        _db = None


def load_config() -> dict:
    """Load configuration from feeds.yaml."""
    config_file = Path(_CONFIG_PATH)
    if not config_file.exists():
        return {"feeds": [], "settings": {}}
    with open(config_file, "r") as f:
        return yaml.safe_load(f) or {"feeds": [], "settings": {}}


def save_config(config: dict):
    """Write configuration to feeds.yaml."""
    config_file = Path(_CONFIG_PATH)
    config_file.parent.mkdir(parents=True, exist_ok=True)
    with open(config_file, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)


# Settings each user sets for themselves. A user without their own LLM or
# episode limit gets the shared setting of the same name, then these
# defaults. The rest are personal only: a value in the shared config is
# ignored, so e.g. no daily run starts for a user who never asked for one.
USER_SETTING_DEFAULTS: dict[str, Any] = {
    "language": "en",  # "en" or "fr" (Quebec French): what pages show first
    "tone": "",  # instructions describing the user's voice, sent to the writer
    "llm_provider": "ollama",  # LLM for the user's writing: posts, synopses
    "llm_model": None,
    "gemini_model": None,
    "max_episodes_per_feed": 10,
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


def content_dir(base: str, username: Optional[str]) -> Path:
    """Folder for a user's generated files (posts, exports): ``base/<user>``.
    Without a username, ``base`` itself (CLI output)."""
    return Path(base) / username if username else Path(base)
