"""Settings management routes."""

from fastapi import APIRouter

from p3.api.deps import load_config, save_config
from p3.api.models import SettingsOut, SettingsUpdate

router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("", response_model=SettingsOut)
def get_settings():
    config = load_config()
    return SettingsOut(
        feeds=config.get("feeds", []),
        settings=config.get("settings", {}),
    )


@router.put("", response_model=SettingsOut)
def update_settings(body: SettingsUpdate):
    config = load_config()
    if body.feeds is not None:
        config["feeds"] = body.feeds
    if body.settings is not None:
        config["settings"] = body.settings
    save_config(config)
    return SettingsOut(
        feeds=config.get("feeds", []),
        settings=config.get("settings", {}),
    )
