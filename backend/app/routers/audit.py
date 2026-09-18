# backend/app/routers/audit.py
"""Paginated, filterable audit log endpoint."""

from typing import Optional

from fastapi import APIRouter, Query
from fastapi.responses import Response

from app.db import query_db

router = APIRouter(prefix="/api/audit", tags=["audit"])


def _db_path() -> str:
    """Resolve DB_PATH at call time so tests can monkeypatch app.main.DB_PATH."""
    from app.main import DB_PATH
    return DB_PATH


@router.get("")
def get_audit_log(
    kind: Optional[str] = Query(default=None),
    user: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=500),
):
    """Return a paginated, optionally filtered audit log.

    Query params:
        kind:     Filter by event kind (run, label, config, threshold, upload, export, model).
        user:     Filter by user_name (exact match).
        page:     1-based page number (default 1).
        per_page: Rows per page (default 50, max 500).

    Response:
        {"items": [...], "total": N, "page": P, "per_page": PP}
    """
    db_path = _db_path()

    # Build WHERE clause from optional filters
    conditions: list[str] = []
    params: list = []

    if kind is not None:
        conditions.append("kind = ?")
        params.append(kind)
    if user is not None:
        conditions.append("user_name = ?")
        params.append(user)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    # Total count
    count_rows = query_db(
        db_path,
        f"SELECT COUNT(*) AS cnt FROM audit_log {where}",
        tuple(params),
    )
    total: int = count_rows[0]["cnt"]

    # Paginated rows, newest first
    offset = (page - 1) * per_page
    items = query_db(
        db_path,
        f"SELECT * FROM audit_log {where} ORDER BY timestamp DESC, id DESC LIMIT ? OFFSET ?",
        tuple(params + [per_page, offset]),
    )

    return {"items": items, "events": items, "total": total, "page": page, "per_page": per_page}


@router.get("/counts")
def get_audit_counts():
    db_path = _db_path()
    rows = query_db(
        db_path,
        "SELECT kind, COUNT(*) AS cnt FROM audit_log GROUP BY kind",
    )
    counts = {row["kind"]: row["cnt"] for row in rows}
    counts["all"] = sum(counts.values())
    return counts


@router.get("/export.jsonl")
def export_audit_jsonl(
    kind: Optional[str] = Query(default=None),
    user: Optional[str] = Query(default=None),
):
    db_path = _db_path()
    conditions: list[str] = []
    params: list = []

    if kind is not None:
        conditions.append("kind = ?")
        params.append(kind)
    if user is not None:
        conditions.append("user_name = ?")
        params.append(user)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    rows = query_db(
        db_path,
        f"SELECT * FROM audit_log {where} ORDER BY timestamp DESC, id DESC",
        tuple(params),
    )
    import json

    content = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows)
    if content:
        content += "\n"
    return Response(
        content=content,
        media_type="application/x-ndjson",
        headers={"Content-Disposition": "attachment; filename=audit_log.jsonl"},
    )
