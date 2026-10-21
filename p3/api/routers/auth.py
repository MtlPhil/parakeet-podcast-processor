"""Built-in sign-in (P3_AUTH=local): status, first-run admin account,
sign-in and out, passwords, and admin-created accounts. See p3/api/auth.py.

In proxy mode only ``/api/auth/status`` is useful: the proxy owns accounts.
"""

import threading
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from p3.api import auth
from p3.api.auth import (
    auth_mode,
    check_new_password,
    current_user,
    hash_password,
    require_admin,
    require_local_mode,
    valid_username,
    verify_password,
)
from p3.api.deps import get_db
from p3.api.models import UserOut

router = APIRouter(tags=["auth"])

# One account creation at a time, so two first-run setups can't both win.
_create_lock = threading.Lock()

# Slows password guessing; a failed sign-in waits this long before answering.
_FAILED_LOGIN_DELAY = 1.0


class AuthStatus(BaseModel):
    mode: str
    setup_needed: bool
    signed_in: bool
    busy: bool = Field(description="Jobs are pending or running")


class AccountIn(BaseModel):
    username: str
    password: str
    display_name: Optional[str] = None
    email: Optional[str] = None


class NewUserIn(AccountIn):
    role: str = Field("user", pattern="^(admin|user)$")


class LoginIn(BaseModel):
    username: str
    password: str


class PasswordChange(BaseModel):
    current_password: str
    new_password: str


class PasswordReset(BaseModel):
    password: str


def _create_account(body: AccountIn, role: Optional[str]) -> dict:
    """Create a local account. ``role`` None means the database's rule (the
    first user is an admin)."""
    username = body.username.strip()
    if not valid_username(username):
        raise HTTPException(
            400,
            "Usernames use letters, digits and . _ @ - (start with a letter or digit)",
        )
    check_new_password(body.password)
    db = get_db()
    if db.get_user(username):
        raise HTTPException(409, "That username is taken")
    user = db.ensure_user(
        username, (body.display_name or "").strip() or None, (body.email or "").strip() or None
    )
    if role and user["role"] != role:
        db.set_user_role(username, role)
    db.set_password_hash(username, hash_password(body.password))
    created = db.get_user(username)
    assert created is not None
    return created


@router.get("/api/auth/status", response_model=AuthStatus)
def status(request: Request):
    """Public: whether this install has its own accounts, needs its first
    one, and whether this browser is signed in."""
    db = get_db()
    mode = auth_mode()
    if mode == "local":
        setup_needed = db.count_users() == 0
        signed_in = auth.session_username(request) is not None
    else:
        setup_needed = False
        signed_in = bool(request.headers.get(auth.USER_HEADER))
    return {
        "mode": mode,
        "setup_needed": setup_needed,
        "signed_in": signed_in,
        "busy": bool(db.get_active_jobs()),
    }


@router.post(
    "/api/auth/setup", response_model=UserOut, dependencies=[Depends(require_local_mode)]
)
def create_first_admin(body: AccountIn, response: Response):
    """First run only: create the admin account and sign it in."""
    with _create_lock:
        if get_db().count_users() > 0:
            raise HTTPException(409, "Setup is already done; sign in instead")
        user = _create_account(body, "admin")
    auth.start_session(response, user["username"])
    return user


@router.post(
    "/api/auth/login", response_model=UserOut, dependencies=[Depends(require_local_mode)]
)
def login(body: LoginIn, response: Response):
    db = get_db()
    username = body.username.strip()
    if not verify_password(body.password, db.get_password_hash(username)):
        time.sleep(_FAILED_LOGIN_DELAY)
        raise HTTPException(401, "Wrong username or password")
    auth.start_session(response, username)
    user = db.get_user(username)
    assert user is not None
    return user


@router.post("/api/auth/logout", response_model=dict)
def logout(request: Request, response: Response):
    auth.end_session(request, response)
    return {"signed_out": True}


@router.post(
    "/api/me/password", response_model=dict, dependencies=[Depends(require_local_mode)]
)
def change_my_password(
    body: PasswordChange, request: Request, user: dict = Depends(current_user)
):
    """Change your own password; your other sessions are signed out."""
    db = get_db()
    if not verify_password(body.current_password, db.get_password_hash(user["username"])):
        time.sleep(_FAILED_LOGIN_DELAY)
        raise HTTPException(400, "Current password is wrong")
    check_new_password(body.new_password)
    db.set_password_hash(user["username"], hash_password(body.new_password))
    db.delete_user_sessions(user["username"], keep=auth.session_hash(request))
    return {"changed": True}


@router.post(
    "/api/admin/users",
    response_model=UserOut,
    dependencies=[Depends(require_local_mode)],
)
def create_user(body: NewUserIn, _admin: dict = Depends(require_admin)):
    """An admin creates an account, as admin or user."""
    with _create_lock:
        return _create_account(body, body.role)


@router.put(
    "/api/admin/users/{username}/password",
    response_model=dict,
    dependencies=[Depends(require_local_mode)],
)
def reset_password(
    username: str, body: PasswordReset, _admin: dict = Depends(require_admin)
):
    """An admin sets someone's password; their sessions are signed out."""
    db = get_db()
    if not db.get_user(username):
        raise HTTPException(404, "User not found")
    check_new_password(body.password)
    db.set_password_hash(username, hash_password(body.password))
    db.delete_user_sessions(username)
    return {"reset": True}
