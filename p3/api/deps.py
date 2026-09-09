"""Shared dependencies for the API."""

from pathlib import Path
from typing import Generator

import yaml

from p3.database import P3Database

_DB_PATH = "data/p3.duckdb"
_CONFIG_PATH = "config/feeds.yaml"

# Singleton database instance (DuckDB supports concurrent reads from one connection)
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
