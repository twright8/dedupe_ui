# backend/app/auth.py
"""Auth router + middleware: site password login, user picker, session cookies."""

import json
import os
import secrets

from fastapi import APIRouter, Cookie, HTTPException, Request, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.db import query_db, write_db

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SITE_PASSWORD = os.environ.get("SITE_PASSWORD", "")
if not SITE_PASSWORD:
    raise RuntimeError("SITE_PASSWORD environment variable is required")

SECRET_KEY = os.environ.get("SECRET_KEY", secrets.token_hex(32))
SESSION_MAX_AGE = 60 * 60 * 24 * 7  # 7 days

_serializer = URLSafeTimedSerializer(SECRET_KEY)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _db_path() -> str:
    """Resolve DB_PATH at call time so tests can patch DATA_DIR."""
    from app.main import DB_PATH
    return DB_PATH


def _sign(payload: dict) -> str:
    return _serializer.dumps(payload)


def _unsign(token: str, max_age: int = SESSION_MAX_AGE) -> dict | None:
    try:
        return _serializer.loads(token, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None


def current_user(user: str = Cookie(default=None)) -> str:
    """Best-effort display name for audit attribution.

    The session middleware proves the request is authenticated. The separate
    user cookie is honor-system attribution; tests and early API clients may
    not set it, so fall back to "user" instead of failing mutations.
    """
    if not user:
        return "user"
    data = _unsign(user)
    if not data:
        return "user"
    return data.get("name") or "user"


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    password: str


class SetUserRequest(BaseModel):
    name: str
    initials: str


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login")
def login(body: LoginRequest, response: Response):
    if body.password != SITE_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid password")
    token = _sign({"authenticated": True})
    response.set_cookie(
        key="session",
        value=token,
        httponly=True,
        samesite="lax",
        max_age=SESSION_MAX_AGE,
    )
    return {"ok": True}


@router.post("/logout")
def logout(response: Response):
    """Clear the session and user cookies so the next request is unauthenticated."""
    response.delete_cookie("session", samesite="lax")
    response.delete_cookie("user", samesite="lax")
    return {"ok": True}


@router.get("/me")
def me(session: str = Cookie(None), user: str = Cookie(None)):
    if not session or not _unsign(session):
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not user:
        raise HTTPException(status_code=401, detail="No user set")
    data = _unsign(user)
    if not data:
        raise HTTPException(status_code=401, detail="Invalid user cookie")
    return {"name": data["name"], "initials": data["initials"]}


@router.post("/set-user")
def set_user(
    body: SetUserRequest,
    response: Response,
    session: str = Cookie(None),
):
    if not session or not _unsign(session):
        raise HTTPException(status_code=401, detail="Not authenticated")
    # Upsert into users table
    write_db(
        _db_path(),
        """INSERT INTO users (name, initials) VALUES (?, ?)
           ON CONFLICT(name) DO UPDATE SET initials = excluded.initials""",
        (body.name, body.initials),
    )
    token = _sign({"name": body.name, "initials": body.initials})
    response.set_cookie(
        key="user",
        value=token,
        httponly=True,
        samesite="lax",
        max_age=SESSION_MAX_AGE,
    )
    return {"ok": True}


@router.get("/users")
def list_users(session: str = Cookie(None)):
    if not session or not _unsign(session):
        raise HTTPException(status_code=401, detail="Not authenticated")
    rows = query_db(_db_path(), "SELECT name, initials FROM users ORDER BY name")
    return rows


# ---------------------------------------------------------------------------
# Middleware — protect /api/* (except /api/auth/login and /api/health)
# ---------------------------------------------------------------------------

# /api/profile is public like /api/health: the frontend reads the title, base
# path and column set before the login screen is drawn.
_PUBLIC_PATHS = {"/api/auth/login", "/api/auth/logout", "/api/health", "/api/profile"}


class SessionMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/") and path not in _PUBLIC_PATHS:
            token = request.cookies.get("session")
            if not token or not _unsign(token):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Not authenticated"},
                )
        return await call_next(request)
