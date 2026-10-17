"""Shared settings (config/feeds.yaml): admins only.

These cover work done once per episode for everyone: transcription, audio
format, the LLM that cleans and summarizes. Each user's own settings are
under /api/me/settings.
"""

from fastapi import APIRouter, Depends

from p3.api.auth import require_admin
from p3.api.deps import load_config, save_config
from p3.api.models import SettingsOut, SettingsUpdate

router = APIRouter(
    prefix="/api/settings", tags=["settings"], dependencies=[Depends(require_admin)]
)


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
