"""Who is making a request.

Two modes, chosen by the ``P3_AUTH`` environment variable:

``proxy`` (default): P3 sits behind a reverse proxy (e.g. Traefik) that
authenticates every request with Authelia's forward-auth and passes the
signed-in user in the ``Remote-User`` header (plus ``Remote-Name`` and
``Remote-Email``). P3 trusts that header, so it must only be reachable
through the proxy, and the proxy must overwrite any client-supplied copy
(Traefik's ``authResponseHeaders`` does). For local development without a
proxy, set ``P3_DEV_USER`` to the username to act as. It is used only when
the header is absent.

``local``: no proxy (the Mac installer's mode). P3 has its own accounts:
users sign in with a password and get a session cookie. The ``Remote-User``
header and ``P3_DEV_USER`` are ignored, since any client on the network
could send them. The first account, created by the setup wizard, is an
admin; admins create the others (see routers/auth.py).
"""

import hashlib
import hmac
import os
import re
import secrets
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from fastapi import Depends, HTTPException, Request, Response

from p3.api.deps import get_db

USER_HEADER = "Remote-User"
NAME_HEADER = "Remote-Name"
EMAIL_HEADER = "Remote-Email"
DEV_USER_ENV = "P3_DEV_USER"
AUTH_MODE_ENV = "P3_AUTH"

SESSION_COOKIE = "p3_session"
SESSION_DAYS = 30
MIN_PASSWORD_LENGTH = 8

# Usernames name per-user content folders, so keep them path-safe.
_USERNAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]{0,63}")

# scrypt parameters (RFC 7914's interactive-login recommendation).
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1


def auth_mode() -> str:
    """``local`` or ``proxy`` (anything else counts as proxy)."""
    return "local" if os.environ.get(AUTH_MODE_ENV, "").lower() == "local" else "proxy"


def valid_username(username: str) -> bool:
    return bool(_USERNAME_RE.fullmatch(username or ""))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: Optional[str]) -> bool:
    if not stored:
        return False
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        candidate = hashlib.scrypt(
            password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate.hex(), digest)


def check_new_password(password: str):
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise HTTPException(
            400, f"Password must be at least {MIN_PASSWORD_LENGTH} characters"
        )


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def start_session(response: Response, username: str):
    """Sign ``username`` in: store a new session and set its cookie."""
    token = secrets.token_urlsafe(32)
    get_db().create_session(
        _token_hash(token), username, datetime.now() + timedelta(days=SESSION_DAYS)
    )
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_DAYS * 24 * 3600,
        httponly=True,
        samesite="lax",
        path="/",
    )


def end_session(request: Request, response: Response):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        get_db().delete_session(_token_hash(token))
    response.delete_cookie(SESSION_COOKIE, path="/")


def session_hash(request: Request) -> Optional[str]:
    token = request.cookies.get(SESSION_COOKIE)
    return _token_hash(token) if token else None


def session_username(request: Request) -> Optional[str]:
    """The user signed in with this request's session cookie, if any."""
    token_hash = session_hash(request)
    if not token_hash:
        return None
    return get_db().get_session_username(token_hash, datetime.now())


def current_user(request: Request) -> Dict[str, Any]:
    """The signed-in user's row (in proxy mode, created on first request)."""
    if auth_mode() == "local":
        username = session_username(request)
        user = get_db().get_user(username) if username else None
        if not user:
            raise HTTPException(401, "Not signed in")
        return user

    username = (
        request.headers.get(USER_HEADER) or os.environ.get(DEV_USER_ENV) or ""
    ).strip()
    if not username:
        raise HTTPException(401, "Not signed in")
    if not valid_username(username):
        raise HTTPException(400, "Unsupported username")
    return get_db().ensure_user(
        username,
        request.headers.get(NAME_HEADER) or None,
        request.headers.get(EMAIL_HEADER) or None,
    )


def current_username(user: Dict[str, Any] = Depends(current_user)) -> str:
    return user["username"]


def require_admin(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    if user["role"] != "admin":
        raise HTTPException(403, "Admins only")
    return user


def require_local_mode():
    if auth_mode() != "local":
        raise HTTPException(
            400, "Accounts are managed by the reverse proxy in this installation"
        )
