# backend/app/routers/notes.py
"""Shared app-wide notes (currently: the methodology notes on the review screen).

Stored in the ``app_settings`` key/value table, so the note is the same for every
user. HTML is sanitised on save; ``updated_by`` is taken from the signed user
cookie for a light audit trail.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.auth import current_user
from app.db import query_db, write_db
from app.services.audit_logger import log_event
from app.services.html_sanitizer import sanitize_html

router = APIRouter(prefix="/api/notes", tags=["notes"])

_METHODOLOGY_KEY = "review_methodology_notes"


class NotesBody(BaseModel):
    content: str


def _db_path() -> str:
    from app.main import DB_PATH
    return DB_PATH


def _read(key: str) -> dict:
    rows = query_db(
        _db_path(),
        "SELECT value, updated_at, updated_by FROM app_settings WHERE key = ?",
        (key,),
    )
    if not rows:
        return {"content": "", "updated_at": None, "updated_by": None}
    row = rows[0]
    return {
        "content": row["value"] or "",
        "updated_at": row["updated_at"],
        "updated_by": row["updated_by"],
    }


@router.get("/methodology")
def get_methodology():
    return _read(_METHODOLOGY_KEY)


@router.put("/methodology")
def put_methodology(body: NotesBody, who: str = Depends(current_user)):
    clean = sanitize_html(body.content)
    before = _read(_METHODOLOGY_KEY)
    write_db(
        _db_path(),
        """INSERT INTO app_settings (key, value, updated_at, updated_by)
           VALUES (?, ?, datetime('now'), ?)
           ON CONFLICT(key) DO UPDATE SET
               value = excluded.value,
               updated_at = excluded.updated_at,
               updated_by = excluded.updated_by""",
        (_METHODOLOGY_KEY, clean, who),
    )
    # The notes explain the method to everyone who reads a review screen, so a
    # change to them is a change to the method (docs/TERMINOLOGY_AUDIT.md,
    # gap 9). `app_settings` keeps no history; the audit log does.
    log_event(
        _db_path(), user=who or "unknown", kind="config",
        description="Saved the methodology notes",
        metadata={"key": _METHODOLOGY_KEY, "characters": len(clean),
                  "was_characters": len(before.get("content") or "")},
    )
    return _read(_METHODOLOGY_KEY)
