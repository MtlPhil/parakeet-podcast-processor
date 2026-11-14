"""The signed-in user's profile and settings, and user administration."""

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from p3.api.auth import current_user, end_session, require_admin
from p3.api.deps import delete_content, get_db, user_settings
from p3.api.models import (
    DeletedEpisodeAllow,
    DeletedEpisodeOut,
    MeOut,
    RoleUpdate,
    UserOut,
    UserSettings,
)
from p3.api.scheduler import parse_run_time
from p3.api.tasks import daily_run

router = APIRouter(prefix="/api", tags=["users"])


def _delete_user(user: dict):
    """Delete a user, unless they are the only admin (someone must be able to
    manage the shared settings, and the next sign-in would otherwise take over
    the whole library as the first user) or work of theirs is under way."""
    db = get_db()
    username = user["username"]
    admins = [u for u in db.get_all_users() if u["role"] == "admin"]
    if admins == [user]:
        raise HTTPException(
            400, "The only admin can't be deleted. Make someone else an admin first."
        )
    run_active = (
        db.get_state(daily_run.run_key("id", username)) in daily_run.active_run_ids
    )
    if run_active or db.get_active_jobs(username):
        raise HTTPException(
            409, f"{username} has jobs in progress. Try again when they finish."
        )
    db.delete_user(username)
    delete_content(username)


def _me(user: dict) -> dict:
    return {
        **user,
        "settings": user_settings(user["username"]),
        "own_settings": user["settings"],
    }


@router.get("/me", response_model=MeOut)
def get_me(user: dict = Depends(current_user)):
    return _me(user)


@router.delete("/me", response_model=dict)
def delete_my_account(
    request: Request, response: Response, user: dict = Depends(current_user)
):
    """Delete the signed-in user's account and sign them out."""
    _delete_user(user)
    end_session(request, response)
    return {"deleted": True}


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


@router.get("/me/deleted-episodes", response_model=list[DeletedEpisodeOut])
def list_deleted_episodes(user: dict = Depends(current_user)):
    """Episodes the user deleted; fetches skip them."""
    return get_db().get_deleted_episodes(user["username"])


@router.post("/me/deleted-episodes/allow", response_model=dict)
def allow_deleted_episode(
    body: DeletedEpisodeAllow, user: dict = Depends(current_user)
):
    """Take an episode off the deleted list, so a later fetch can bring it
    back (it is downloaded and processed again, not restored)."""
    if not get_db().allow_deleted_episode(user["username"], body.url):
        raise HTTPException(404, "Episode is not on your deleted list")
    return {"allowed": True}


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


@router.delete("/admin/users/{username}", response_model=dict)
def delete_user(username: str, _admin: dict = Depends(require_admin)):
    """Delete someone's account."""
    target = get_db().get_user(username)
    if not target:
        raise HTTPException(404, "User not found")
    _delete_user(target)
    return {"deleted": True}
