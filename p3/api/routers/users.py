"""The signed-in user's profile and settings, and user administration."""

from fastapi import APIRouter, Depends, HTTPException

from p3.api.auth import current_user, require_admin
from p3.api.deps import get_db, user_settings
from p3.api.models import MeOut, RoleUpdate, UserOut, UserSettings
from p3.api.scheduler import parse_run_time

router = APIRouter(prefix="/api", tags=["users"])


def _me(user: dict) -> dict:
    return {
        **user,
        "settings": user_settings(user["username"]),
        "own_settings": user["settings"],
    }


@router.get("/me", response_model=MeOut)
def get_me(user: dict = Depends(current_user)):
    return _me(user)


@router.put("/me/settings", response_model=MeOut)
def update_my_settings(body: UserSettings, user: dict = Depends(current_user)):
    """Replace the user's own settings. Fields left out or empty fall back
    to the shared settings."""
    if body.daily_run_time and parse_run_time(body.daily_run_time) is None:
        raise HTTPException(400, "daily_run_time must be HH:MM")
    settings = {
        key: value
        for key, value in body.model_dump().items()
        if value is not None and (value != "" or key in ("tone", "daily_run_time"))
    }
    db = get_db()
    db.update_user_settings(user["username"], settings)
    updated = db.get_user(user["username"])
    assert updated is not None
    return _me(updated)


@router.get("/admin/users", response_model=list[UserOut])
def list_users(_admin: dict = Depends(require_admin)):
    return get_db().get_all_users()


@router.patch("/admin/users/{username}", response_model=UserOut)
def set_role(username: str, body: RoleUpdate, _admin: dict = Depends(require_admin)):
    """Make a user an admin or a regular user. The last admin can't be
    demoted, so someone can always manage the shared settings."""
    db = get_db()
    target = db.get_user(username)
    if not target:
        raise HTTPException(404, "User not found")
    admins = [u for u in db.get_all_users() if u["role"] == "admin"]
    if body.role == "user" and admins == [target]:
        raise HTTPException(400, "There must be at least one admin")
    db.set_user_role(username, body.role)
    updated = db.get_user(username)
    assert updated is not None
    return updated
