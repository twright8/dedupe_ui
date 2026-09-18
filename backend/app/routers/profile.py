# backend/app/routers/profile.py
"""Profile API — what this instance is and which columns it shows.

Public, like /api/health: the frontend reads it before the login screen so the
page can carry the right title and base path.
"""

from fastapi import APIRouter

from app.base_path import base_path
from app.profiles import get_profile

router = APIRouter(prefix="/api/profile", tags=["profile"])


@router.get("")
def read_profile():
    return {**get_profile().as_dict(), "base_path": base_path()}
