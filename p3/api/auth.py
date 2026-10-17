"""Who is making a request.

P3 sits behind a reverse proxy (e.g. Traefik) that authenticates every
request with Authelia's forward-auth and passes the signed-in user in the
``Remote-User`` header (plus ``Remote-Name`` and ``Remote-Email``). P3
trusts that header, so it must only be reachable through the proxy, and the
proxy must overwrite any client-supplied copy (Traefik's
``authResponseHeaders`` does).

For local development without a proxy, set ``P3_DEV_USER`` to the username
to act as. It is used only when the header is absent.
"""

import os
import re
from typing import Any, Dict

from fastapi import Depends, HTTPException, Request

from p3.api.deps import get_db

USER_HEADER = "Remote-User"
NAME_HEADER = "Remote-Name"
EMAIL_HEADER = "Remote-Email"
DEV_USER_ENV = "P3_DEV_USER"

# Usernames name per-user content folders, so keep them path-safe.
_USERNAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]{0,63}")


def current_user(request: Request) -> Dict[str, Any]:
    """The signed-in user's row, created on their first request."""
    username = (
        request.headers.get(USER_HEADER) or os.environ.get(DEV_USER_ENV) or ""
    ).strip()
    if not username:
        raise HTTPException(401, "Not signed in")
    if not _USERNAME_RE.fullmatch(username):
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
